from osgeo import gdal,osr
import h5py
import os
import datetime as dt
import numpy as np

def hdf_read(input_hdf):

    DATA_ROOT = 'science/SENTINEL1'
    grid_path = f'{DATA_ROOT}/CSLC/grids'
    metadata_path = f'{DATA_ROOT}/CSLC/metadata'
    burstmetadata_path = f'{DATA_ROOT}/CSLC/metadata/processing_information/s1_burst_metadata'
    id_path = f'{DATA_ROOT}/identification'
    pol = 'VV'

    with h5py.File(input_hdf,'r') as h5:
        xcoor = h5[f'{grid_path}/x_coordinates'][:]
        ycoor = h5[f'{grid_path}/y_coordinates'][:]
        dx = h5[f'{grid_path}/x_spacing'][()].astype(int)
        dy = h5[f'{grid_path}/y_spacing'][()].astype(int)
        epsg = h5[f'{grid_path}/projection'][()].astype(int)
        slc = h5[f'{grid_path}/{pol}'][:]
        sensing_start = h5[f'{burstmetadata_path}/sensing_start'][()].astype(str)
        date = dt.datetime.strptime(sensing_start.astype(str),'%Y-%m-%d %H:%M:%S.%f').strftime('%Y%m%d')
    return xcoor, ycoor, dx, dy, epsg, slc, date

def convert_to_slcvrt(xcoor, ycoor, dx, dy, epsg, slc, date, outdir):

     os.makedirs(outdir,exist_ok=True)

     height, width = slc.shape

     slc_file = outdir + '/' + date+'.slc'
     slc_vrt = slc_file+'.vrt'

     outtype = '<f'  #little endian (float)
     dtype = gdal.GDT_CFloat32
     drvout = gdal.GetDriverByName('ENVI')
     raster_out = drvout.Create(slc_file, width,height, 1, dtype)
     raster_out.SetGeoTransform([xcoor[0],dx,0.0,ycoor[0],0.0,dy])

     srs = osr.SpatialReference()
     srs.ImportFromEPSG(int(epsg))
     raster_out.SetProjection(srs.ExportToWkt())

     band_out = raster_out.GetRasterBand(1)
     band_out.WriteArray(slc)
     band_out.FlushCache()
     del band_out

     command = 'gdal_translate ' + slc_file + ' ' + slc_vrt + f' > {outdir}/tmp.LOG'
     os.system(command)

def array2raster(outrasterfile,OriginX, OriginY, pixelWidth,pixelHeight,epsg,array):
    #generating geotiff file from 2D array

    cols = array.shape[1]
    rows = array.shape[0]
    originX = OriginX
    originY = OriginY

    driver = gdal.GetDriverByName('ENVI')
    outRaster = driver.Create(outrasterfile, cols, rows, 1, gdal.GDT_Float32)
    outRaster.SetGeoTransform((originX, pixelWidth, 0, originY, 0, pixelHeight))
    outband = outRaster.GetRasterBand(1)
    outband.WriteArray(array)
    outRasterSRS = osr.SpatialReference()
    outRasterSRS.ImportFromEPSG(epsg)
    outRaster.SetProjection(outRasterSRS.ExportToWkt())
    outband.FlushCache()

def simple_SBAS_stats(offlist,snrlist,out_dir,snr_thr):
    #offlist: offset filelist
    #snrlist: snr filelist
    #out_dir: output directory
    #snr_thr: snr threshold

    num_pairs = offlist.shape[0]

    refd = []
    secd = []

    for _ in offlist:
        refd.append(_[0:8])
        secd.append(_[9:17])

    days = refd + secd
    days = (np.unique(sorted(days))).tolist()
    n_days = len(days)      #number of unique days

    #building a design matrix
    D = np.zeros((num_pairs,n_days))   #initialization

    for ii in range(num_pairs):
        ref_index = days.index(refd[ii])
        sec_index = days.index(secd[ii])
        D[ii,ref_index] = -1
        D[ii,sec_index] = 1

    invD = np.linalg.pinv(D)  #inverse of a Design matrix

    #opening first tiff file for obtaining parameters
    _ = out_dir + '/' + offlist[0] 
    ds = gdal.Open(_, gdal.GA_ReadOnly)
    _ = ds.GetRasterBand(1).ReadAsArray()
    row, col = _.shape
    transform = ds.GetGeoTransform()
    minX = transform[0]
    maxY = transform[3]
    spacingX = transform[1]
    spacingY = transform[5]
    maxX = minX + (col-1)*spacingX
    minY = maxY + (row-1)*spacingY
    proj = ds.GetProjection()
    _ = None

    off_3d = np.zeros((row,col,num_pairs))

    for ii, (offF,snrF) in enumerate(zip(offlist, snrlist)):
        offFile = out_dir + '/' + offF
        snrFile = out_dir + '/' + snrF

        ds = gdal.Open(snrFile, gdal.GA_ReadOnly)
        snr = ds.GetRasterBand(1).ReadAsArray()

        ds = gdal.Open(offFile, gdal.GA_ReadOnly)
        off = ds.GetRasterBand(1).ReadAsArray()
        off[snr<snr_thr] = np.nan
        off_3d[:,:,ii] = off

    #SBAS inversion for time-series estimates
    ts_off = np.einsum('ijk,lk->ijl', off_3d, invD)
    norm_res_off = np.sqrt(np.sum((off_3d - np.einsum('ijk,lk->ijl', ts_off, D))**2,axis = 2))  #L2 norm residual

    #removing pixels with a large L2 norm residual
    normResThr = np.nanmin(norm_res_off) + (np.nanmax(norm_res_off) 
                                              - np.nanmin(norm_res_off))*0.75
                                              #threshold of L2 norm residual #np.nanquantile(norm_res_off, 0.75)   
    indRes = (norm_res_off>normResThr)
    off_3d[indRes,:] = np.nan

    first_ts_off = ts_off[:,:,0]
    ts_off_all = dict()

    for ii, ID in enumerate(days):
        dat = ts_off[:,:,ii] - first_ts_off     #first data becomes zero
        ts_off_all[ID] = dat    #time-series range offset

    _avg = []
    _std = []

    for day in days:
        _avg.append(np.nanmean(ts_off_all[day]))
        _std.append(np.nanstd(ts_off_all[day]))

    return _avg, _std, days