import os, sys
import threading
import wx
import PIL
from PIL import Image
from os.path import join as pathjoin
import specify_voting_targets.util_widgets as widgets
from wx.lib.pubsub import Publisher
import time
import threshold.imageFile
from util import MyGauge, get_filename, create_dirs, is_image_ext, is_multipage
import pdb
import array
import csv
import pickle

#sys.path.append('../pixel_reg')
from pixel_reg.doExtract import convertImagesSingleMAP, convertImagesMultiMAP, encodepath
from pixel_reg.shared import standardImread

TIMER = None # set by MainFrame

class RunTargets(wx.Panel):
    def __init__(self, parent, _TIMER=None):
        wx.Panel.__init__(self, parent, id=-1) 
        global TIMER
        TIMER = _TIMER
        self.sizer = wx.BoxSizer(wx.VERTICAL)

        self.startbutton = wx.Button(self, label="Go!")
        self.startbutton.Bind(wx.EVT_BUTTON, lambda x: self.start())

        self.startbutton_debug = wx.Button(self, label="Go, but don't reprocess.")
        self.startbutton_debug.Bind(wx.EVT_BUTTON, lambda x: self.start(rerun=False))
        self.startbutton_debug.Hide()

        self.sizer.Add(self.startbutton)
        self.sizer.Add(self.startbutton_debug)

        self.SetSizer(self.sizer)

        Publisher().subscribe(self.getproj, "broadcast.project")
        Publisher().subscribe(self._pubsub_rundone, "broadcast.rundone")
        
    def _pubsub_rundone(self, msg):
        try:
            self.TIMER.stop_task(('cpu', 'Target Extraction Computation'))
        except:
            print "RunTargets couldn't output to TIMER."
    
    def set_timer(self, timer):
        self.TIMER = timer
        global TIMER
        TIMER = timer

    def getproj(self, msg):
        self.proj = msg.data
        if self.proj.options.devmode:
            self.startbutton_debug.Show()
            self.SendSizeEvent()

    def get_template_paths(self, templatedir):
        """
        Given the directory path that contains the template images,
        returns a (sorted) list of all template image (absolute) paths.
        """
        paths = []
        for dirpath, dirnames, filenames in os.walk(templatedir):
            for imgname in [f for f in filenames if is_image_ext(f)]:
                paths.append(os.path.abspath(os.path.join(dirpath, imgname)))
        return sorted(paths)
        

    def start(self, rerun=True):
        """
        Load up all the images from the file and process them.
        Don't keep them in memory though. That would be bad.
        Just keep around the reference to the image and what value it is.
        """
        imagepath = self.proj.samplesdir
        templatedir = self.proj.templatesdir
        possible = self.get_template_paths(templatedir)

        self.startbutton.Disable()
        self.startbutton_debug.Disable()
        
        r = RunThread(self.proj, imagepath, templatedir, possible, rerun)
        r.start()

        def fn1():
            try:
                return len(os.listdir(self.proj.ballot_metadata))
            except:
                return 0
        fns = [fn1, None, None, None, None, None]

        def enablebuttons():
            self.startbutton.Enable()
            self.startbutton_debug.Enable()

        MyGauge.__bases__ = (wx.Panel,)
        x = MyGauge(self, 5, pos=(000,100), size=(500,800),
                    funs=fns, tofile=self.proj.timing_runtarget,
                    ondone=enablebuttons,
                    ispanel=True, destroyondone=False,
                    thread=r)
        x.Show()
        MyGauge.__bases__ = (wx.Frame,)

class RunThread(threading.Thread):
    def __init__(self, proj, imagepath, templatedir, possible, rerun):
        threading.Thread.__init__(self)
        self.rerun = rerun
        self.imagepath = imagepath
        self.templatedir = templatedir
        self.possible = possible
        self.proj = proj

        self.stop = threading.Event()

    def abort(self):
        self.stop.set()

    def stopped(self):
        return self.stop.isSet()

    def run(self):
        try:
            TIMER.start_task(('cpu', 'Target Extraction Computation'))
        except:
            print "RunThread couldn't write to TIMER."
        imagepath = self.imagepath
        templatedir = self.templatedir
        possible = self.possible

        if not os.path.exists(self.proj.quarantined):
            open(self.proj.quarantined, "w").write("\n")

        count = 0
        for root,dirs,files in os.walk(self.imagepath):
            for f1 in [f for f in files if is_image_ext(f)]:
                count += 1
    
        csvPattern = pathjoin(self.proj.target_locs_dir,'%s_targetlocs.csv')

        # construct pattern for csvpath
        options = map(str,enumerate(possible))

        wx.CallAfter(Publisher().sendMessage, "signals.MyGauge.nextjob", count)

        # check if > 1 template
        if self.rerun:
            if 0: #not is_multipage(self.proj):
                if len(possible)==1:
                    res = convertImagesSingle(imagepath,
                                              pathjoin(templatedir, possible[0]),
                                              csvPattern,
                                              self.proj.extracted_dir, 
                                              self.proj.extracted_metadata,
                                              self.proj.ballot_metadata,
                                              self.proj.quarantined,
                                              self.stopped)
                else:
                    fh=open(self.proj.grouping_results)
                    dreader=csv.DictReader(fh)
                    ballot2group={}
                    for row in dreader:
                        ballot2group[row['samplepath']]=row['templatepath']
                        
                    fh.close()
                    res = convertImagesMulti(imagepath,
                                             ballot2group,
                                             csvPattern,
                                             self.proj.extracted_dir, 
                                             self.proj.extracted_metadata,
                                             self.proj.ballot_metadata,
                                             self.proj.quarantined,
                                             self.stopped)
            else:
                bal2imgs=pickle.load(open(self.proj.ballot_to_images,'rb'))
                tpl2imgs=pickle.load(open(self.proj.template_to_images,'rb'))
                
                if len(tpl2imgs)==1:
                    res = convertImagesSingleMAP(bal2imgs,
                                                 tpl2imgs,
                                                 csvPattern,
                                                 self.proj.extracted_dir, 
                                                 self.proj.extracted_metadata,
                                                 self.proj.ballot_metadata,
                                                 self.proj.quarantined,
                                                 self.stopped)
                else:
                    fh=open(self.proj.grouping_results)
                    dreader=csv.DictReader(fh)
                    bal2tpl={}
                    qfile = open(self.proj.quarantined, 'r')
                    qfiles = set([f.strip() for f in qfile.readlines()])
                    qfile.close()
                    for row in dreader:
                        sample = os.path.abspath(row['samplepath'])
                        if sample not in qfiles:
                            bal2tpl[sample]=row['templatepath']
                    fh.close()
                    res = convertImagesMultiMAP(bal2imgs,
                                                tpl2imgs,
                                                bal2tpl,
                                                csvPattern,
                                                self.proj.extracted_dir, 
                                                self.proj.extracted_metadata,
                                                self.proj.ballot_metadata,
                                                self.proj.quarantined,
                                                self.stopped)
    
            if not res:
                # Was told to abort everything.
                return


        create_dirs(self.proj.extracted_dir)
        dirList = os.listdir(self.proj.extracted_dir)

        dirList = [x for x in dirList if is_image_ext(x)]
        # This will always be a common prefix. 
        # Just add it to there once. Code will be faster.
        dirList = [x for x in dirList]
        
        def doandgetAvg(it):
            wx.CallAfter(Publisher().sendMessage, "signals.MyGauge.tick")
            data = standardImread(pathjoin(self.proj.extracted_dir, it),
                                  flatten=True)
            return 256*(float(sum(map(sum, data))))/(data.size)


        quarantined = set([encodepath(x[:-1]) for x in open(self.proj.quarantined)])

        dirList = [x for x in dirList if os.path.split(x)[1][:os.path.split(x)[1].index(".")] not in quarantined]

        wx.CallAfter(Publisher().sendMessage, "signals.MyGauge.nextjob", len(dirList))

        tmp = zip(dirList, map(doandgetAvg, dirList))

        wx.CallAfter(Publisher().sendMessage, "signals.MyGauge.nextjob", len(dirList))

        fulllst = sorted(tmp, key=lambda x: x[1])
        fulllst = [(x,int(y)) for x,y in fulllst]
        
        #print "FULLLST:", fulllst

        # Find the longest prefix
        prefix = fulllst[0][0]
        for a,_ in fulllst:
            if not a.startswith(prefix):
                new = ""
                for x,y in zip(prefix,a):
                    if x == y:
                        new += x
                    else:
                        break
                prefix = new

        l = len(prefix)
        prefix = pathjoin(self.proj.extracted_dir,prefix)

        open(self.proj.classified+".prefix", "w").write(prefix)
        out = open(self.proj.classified, "w")
        offsets = array.array('L')
        sofar = 0
        for a,b in fulllst:
            line = a[l:] + "\0" + str(b) + "\n"
            out.write(line)
            offsets.append(sofar)
            sofar += len(line)
        out.close()

        offsets.tofile(open(self.proj.classified+".index", "w"))

        print 'done'
        
        threshold.imageFile.makeOneFile(self.proj.extracted_dir, 
                                        fulllst, self.proj.extractedfile)

        wx.CallAfter(Publisher().sendMessage, "broadcast.rundone")
        wx.CallAfter(Publisher().sendMessage, "signals.MyGauge.done")
