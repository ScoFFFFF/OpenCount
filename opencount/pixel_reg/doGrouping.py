from os.path import join as pathjoin
from PIL import Image
from scipy import misc,ndimage
import shared as sh
from imagesAlign import *
import cProfile
import cv
import csv
import os
import string
import sys, shutil
import multiprocessing as mp
from wx.lib.pubsub import Publisher
import json
import wx
import pickle
import time
import random
from util import get_filename, encodepath

def doWrite(finalOrder, Ip, err, attrName, patchDir, metaDir, origfullpath):
    fullpath = encodepath(origfullpath)

    # - patchDir: write out Ip into patchDir
    Ip[np.isnan(Ip)]=1
    to = os.path.join(patchDir, fullpath + '.png')
    sh.imsave(to,Ip)

    # - metaDir:
    # loop over finalOrder and compile array of group IDs and flip/nonflip
    attrOrder=[]; flipOrder=[];
    for pt in finalOrder:
        # pt[2] := (str temppath, str attrval)
        attrOrder.append(pt[2][1])
        flipOrder.append(pt[3])
    
    to = os.path.join(metaDir, fullpath)
    toWrite={"attrOrder": attrOrder, "flipOrder":flipOrder,"err":err}
    file = open(to, "wb")
    pickle.dump(toWrite, file)
    file.close()

def doWriteMAP(finalOrder, Ip, err, attrName, patchDir, metaDir, balKey):
    fullpath = encodepath(balKey)

    # - patchDir: write out Ip into patchDir
    Ip[np.isnan(Ip)]=1
    to = os.path.join(patchDir, fullpath + '.png')
    misc.imsave(to,Ip)

    # - metaDir:
    # loop over finalOrder and compile array of group IDs and flip/nonflip
    attrOrder=[]; imageOrder=[]; flipOrder=[];
    for pt in finalOrder:
        # pt[2] := (str temppath, str attrval)
        attrOrder.append(pt[2])
        imageOrder.append(pt[3])
        flipOrder.append(pt[4])
    
    to = os.path.join(metaDir, fullpath)
    toWrite={"attrOrder": attrOrder, "flipOrder":flipOrder,"err":err,"imageOrder":imageOrder}
    file = open(to, "wb")
    pickle.dump(toWrite, file)
    file.close()

def evalPatchSimilarity(I,patch):
    # perform template matching and return the best match in expanded region
    patchCv=cv.fromarray(np.copy(patch))
    ICv=cv.fromarray(np.copy(I))
    # call template match
    outCv=cv.CreateMat(I.shape[0]-patch.shape[0]+1,I.shape[1]-patch.shape[1]+1,patchCv.type)
    cv.MatchTemplate(ICv,patchCv,outCv,cv.CV_TM_CCOEFF_NORMED)
    Iout=np.asarray(outCv)
    Iout[Iout==1.0]=0;
    YX=np.unravel_index(Iout.argmax(),Iout.shape)

    # local alignment: expand a little, then local align
    i1=YX[0]; i2=YX[0]+patch.shape[0]
    j1=YX[1]; j2=YX[1]+patch.shape[1]
    I1c=I[i1:i2,j1:j2]
    IO=imagesAlign(I1c,patch,type='rigid')

    #return (-IO[2],YX)

    diff=np.abs(IO[1]-patch);
    diff=diff[5:diff.shape[0]-5,5:diff.shape[1]-5];
    # sum values of diffs above  threshold
    err=np.sum(diff[np.nonzero(diff>.25)])
    return (-err,YX,diff)
    
def dist2patches(patchTuples,scale):
    # patchTuples ((K img super regions),(K template patches))
    # for each pair, compute avg distance at scale sc
    scores=np.zeros(len(patchTuples))
    idx=0;
    locs=[]
    for idx in range(len(patchTuples)):
        pt=patchTuples[idx]
        # A fix for a very bizarre openCv bug follows..... [check pixel_reg/opencv_bug_repo.py]
        I=np.round(fastResize(pt[0],scale)*255.)/255.
        # opencv appears to not like pure 1.0 and 0.0 values.
        I[I==1.0]=.999; I[I==0.0]=.001
        patch=np.round(fastResize(pt[1],scale)*255.)/255.
        patch[patch==1.0]=.999; patch[patch==0.0]=.001

        res=evalPatchSimilarity(I,patch)
        scores[idx]=res[0]
        locs.append((res[1][0]/scale,res[1][1]/scale))

    return (scores,locs)

# input: image, patch images, super-region
# output: tuples of cropped image, patch image, template index, and flipped bit
def createPatchTuples(I,attr2pat,R,flip=False):
    pFac=1;
    (rOut,rOff)=sh.expand(R[0],R[1],R[2],R[3],I.shape[0],I.shape[1],pFac)
    I1=I[rOut[0]:rOut[1],rOut[2]:rOut[3]]
 
    patchTuples=[];
    for key in attr2pat.keys():
        # key := (str temppath, str attrval)
        patchTuples.append((I1,attr2pat[key],key,0))

    if not(flip):
        return patchTuples

    Ifl=fastFlip(I)
    Ifl1=Ifl[rOut[0]:rOut[1],rOut[2]:rOut[3]]

    for key in attr2pat.keys():
        # key : (str temppath, str attrval)
        patchTuples.append((Ifl1,attr2pat[key],key,1))

    return patchTuples

def createPatchTuplesMAP(balL,attr2pat,R,flip=False):

    pFac=1;
    patchTuples=[];

    for idx in range(len(balL)):
        balP=balL[idx]
        I=sh.standardImread(balP,flatten=True)
        (rOut,rOff)=sh.expand(R[0],R[1],R[2],R[3],I.shape[0],I.shape[1],pFac)
        I1=I[rOut[0]:rOut[1],rOut[2]:rOut[3]]
        for key in attr2pat.keys():
            # key := (str temppath, str attrval)
            patchTuples.append((I1,attr2pat[key],key,idx,0))

        if flip:
            Ifl=fastFlip(I)
            Ifl1=Ifl[rOut[0]:rOut[1],rOut[2]:rOut[3]]
            for key in attr2pat.keys():
                # key := (str temppath, str attrval)
                patchTuples.append((Ifl1,attr2pat[key],key,idx,1))

    return patchTuples

def templateSSWorker(job):
    (attr2pat, attr2tem, key, superRegion, sStep, minSc, fOut) = job
    # 'key' is (str temppath, str attrval)
    attr2pat1=attr2pat.copy()
    attr2pat1.pop(key)
    #I=sh.standardImread(attr2tem[key],flatten=True)
    I=sh.standardImread(attr2tem[key],flatten=True)
    
    superRegionNp=np.array(superRegion)
    patchTuples=createPatchTuples(I,attr2pat1,superRegionNp,flip=True)

    firstPat=attr2pat1.values()[0]

    sc0=sh.resizeOrNot(firstPat.shape,sh.MAX_PRECINCT_PATCH_DIM)
    (scores0,locs)=dist2patches(patchTuples,sc0)

    sidx=np.argsort(scores0)
    sidx=sidx[::-1]
    trackIdx=sidx[0]

    sc1=sc0-sStep

    while sc1>minSc:
        (scores,locs)=dist2patches(patchTuples,sc1)
        sidx=np.argsort(scores)
        sidx=sidx[::-1]
        mid=np.ceil(len(sidx)/2.0)
        dumpIdx=sidx[mid:len(sidx)]
        if sum(0+(dumpIdx==trackIdx))>0:
            break
        else:
            sc1=sc1-sStep
            
    # write scale to file
    toWrite={"scale": min(sc1+sStep,sc0)}
    file = open(fOut, "wb")
    pickle.dump(toWrite, file)
    file.close()

def groupImagesWorkerMAP(job):
    (attr2pat, superRegion, balKey, balL, scale, destDir, metaDir, attrName) = job

    patchTuples=createPatchTuplesMAP(balL,attr2pat,superRegion,flip=True)
    
    firstPat=attr2pat.values()[0]
    rszFac = sh.resizeOrNot(firstPat.shape,sh.MAX_PRECINCT_PATCH_DIM);
    sweep=np.linspace(scale,rszFac,num=np.ceil(np.log2(len(attr2pat)))+2)

    finalOrder=[]

    # 2. process
    #    Workers:
    #      - align with pyramid + prune
    #      - fine-alignment on best result
    #      - store precinct patch in grouping result folder
    #      - store list in grouping meta result file
    for sc in sweep:
        if len(patchTuples)<2:
            break
        # TODO: handle flipped and unflipped versions differently to save computation
        (scores,locs)=dist2patches(patchTuples,sc)
        sidx=np.argsort(scores)
        # reverse for descend
        sidx=sidx[::-1]
        mid=np.ceil(len(sidx)/2.0)
        bestScore=scores[sidx[0]];
        bestLoc=locs[sidx[0]];
        keepIdx=sidx[0:mid]
        dumpIdx=sidx[mid:len(sidx)]
        dumped=sh.arraySlice(patchTuples,dumpIdx)        
        finalOrder.extend(dumped)
        patchTuples=sh.arraySlice(patchTuples,keepIdx)

    # align patch to top patch
    I1=patchTuples[0][0]
    P1=patchTuples[0][1]
    finalOrder.extend(patchTuples)
    finalOrder.reverse()

    bestLocG=[round(bestLoc[0]),round(bestLoc[1])]
    I1c=I1[bestLocG[0]:bestLocG[0]+P1.shape[0],bestLocG[1]:bestLocG[1]+P1.shape[1]]
    rszFac=sh.resizeOrNot(I1c.shape,sh.MAX_PRECINCT_PATCH_DIM)
    IO=imagesAlign(I1c,P1,type='rigid',rszFac=rszFac)
    doWriteMAP(finalOrder, IO[1], IO[2], attrName , destDir, metaDir, balKey)

def listAttributes(patchesH):
    # tuple ((key=attrType, patchesH tuple))

    attrL = set()
    for val in patchesH.values():
        for (regioncoords, attrtype, attrval, side) in val:
            attrL.add(attrtype)
    
    return list(attrL)

def listAttributesNEW(patchesH):
    # tuple ((key=attrType, patchesH tuple))
    attrMap = {}
    for k in patchesH.keys():
        val=patchesH[k]
        for (bb,attrName,attrVal,side) in val:
            # check if type is in attrMap, if not, create
            if attrMap.has_key(attrName):
                attrMap[attrName][attrVal]=(bb,side,k)
            else:
                attrMap[attrName]={}
                attrMap[attrName][attrVal]=(bb,side,k)                
    return attrMap

def estimateScale(attr2pat,attr2tem,superRegion,initDir,rszFac,stopped):
    print 'estimating scale.'
    jobs=[]
    sStep=.05
    minSc=.1
    sList=[]
    nProc=sh.numProcs()
    for key in attr2pat.keys():
        # key := (str temppath, str attrval)
        #fNm=attr2tem[key]
        jobs.append((attr2pat,attr2tem,key,superRegion,sStep,minSc,pathjoin(initDir,key+'.png')))

    if nProc < 2:
        # default behavior for non multiproc machines
        for job in jobs:
            if stopped():
                return False
            templateSSWorker(job)
    else:
        print 'using ', nProc, ' processes'
        pool=mp.Pool(processes=nProc)

        it = [False]
        def imdone(x):
            it[0] = True
            print "I AM DONE NOW!"
        pool.map_async(templateSSWorker,jobs, callback=lambda x: imdone(it))

        while not it[0]:
            if stopped():
                pool.terminate()
                return False
            time.sleep(.1)

        pool.close()
        pool.join()

    # collect results
    for job in jobs:
        f1=job[6]
        s=pickle.load(open(f1))['scale']
        sList.append(s)

    print sList
    scale=min(max(sList)+4*sStep,rszFac)
    return scale

def groupByAttrMAP(bal2imgs, attrName, attrMap, destDir, metaDir, stopped, verbose=False, deleteall=True):
    """
    options:
        bool deleteall: if True, this will first remove all output files
                         before computing.
    """                       
    
    destDir=destDir+'-'+attrName
    metaDir=metaDir+'-'+attrName

    initDir=metaDir+'_init'
    exmDir=metaDir+'_exemplars'

    if deleteall:
        if os.path.exists(initDir): shutil.rmtree(initDir)
        if os.path.exists(exmDir): shutil.rmtree(exmDir)
        if os.path.exists(destDir): shutil.rmtree(destDir)
        if os.path.exists(metaDir): shutil.rmtree(metaDir)

    create_dirs(destDir)
    create_dirs(metaDir)
    create_dirs(initDir)
    create_dirs(exmDir)

    # maps {(str temppath, str attrval): obj imagepatch}
    attr2pat={}
    # maps {(str temppath, str attrval): str temppath}
    attr2tem={}
    superRegion=(float('inf'),0,float('inf'),0)
    attrValMap=attrMap[attrName]
    for attrVal in attrValMap.keys():
        attrTuple=attrValMap[attrVal]
        bb = attrTuple[0]
        Iref=sh.standardImread(attrTuple[2],flatten=True)
        P=Iref[bb[0]:bb[1],bb[2]:bb[3]]
        attr2pat[attrVal]=P
        attr2tem[attrVal]=attrTuple[2]
        superRegion=sh.bbUnion(superRegion,bb)
        # store exemplar patch
        sh.imsave(pathjoin(exmDir,attrVal+'.png'),P);

    # estimate smallest viable scale
    if len(attr2pat)>2:
        scale = estimateScale(attr2pat,attr2tem,superRegion,initDir,sh.MAX_PRECINCT_PATCH_DIM,stopped)
    else:
        scale = sh.resizeOrNot(P.shape,sh.MAX_PRECINCT_PATCH_DIM);

    print 'ATTR: ', attrName,': using starting scale:',scale

    jobs=[]
    nProc=sh.numProcs()
    for balKey in bal2imgs.keys():
        balL=bal2imgs[balKey]
        jobs.append([attr2pat, superRegion, balKey, balL, scale,
                     destDir, metaDir, attrName])
    
    if nProc < 2:
        # default behavior for non multiproc machines
        for job in jobs:
            if stopped():
                return False
            groupImagesWorkerMAP(job)

    else:
        print 'using ', nProc, ' processes'
        pool=mp.Pool(processes=nProc)

        it = [False]
        def imdone(x):
            it[0] = True
            print "I AM DONE NOW!"
        pool.map_async(groupImagesWorkerMAP,jobs, callback=lambda x: imdone(it))

        while not it[0]:
            if stopped():
                pool.terminate()
                return False
            time.sleep(.1)

        pool.close()
        pool.join()
        
    # TODO: quarantine on grouping errors. For now, just let alignment check handle it
    print 'ATTR: ', attrName, ': done'
    return True

def groupImagesMAP(bal2imgs, tpl2imgs, patchesH, destDir, metaDir, stopped, verbose=False, deleteall=True):
    """
    Input:
      patchesH: A dict mapping:
                  {str imgpath: List of [(y1,y2,x1,x2), str attrtype, str attrval, str side]},
                where 'side' is either 'front' or 'back'.
      ballotD:
      destDir:
      metaDir:
      stopped:
    """
    # NOTE: assuming each ballot has same number of attributes

    # 1. loop over each attribute
    # 2. perform grouping using unique examples of attribute
    # 3. store in metadata folder
    # 4. [verification] look at each attr separately

    # 1. pre-load all template regions
    # Note: because multi-page elections will have different
    # attribute types on the front and back sides, we will have
    # to modify the grouping to accomodate multi-page.


    attrMap=listAttributesNEW(patchesH)

    for attrName in attrMap.keys():
        groupByAttrMAP(bal2imgs,attrName,attrMap,destDir,metaDir,stopped,verbose=verbose,deleteall=deleteall)

def is_image_ext(filename):
    IMG_EXTS = ('.bmp', '.png', '.jpg', '.jpeg', '.tif', '.tiff')
    return os.path.splitext(filename)[1].lower() in IMG_EXTS

def create_dirs(*dirs):
    """
    For each dir in dirs, create the directory if it doesn't yet
    exist. Will work for things like:
        foo/bar/baz
    and will create foo, foo/bar, and foo/bar/baz correctly.
    """
    for dir in dirs:
        try:
            os.makedirs(dir)
        except Exception as e:
            pass