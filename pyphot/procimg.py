""" Module for image processing core methods

"""
import os
import numpy as np

import multiprocessing
from multiprocessing import Process, Queue

from astropy import stats

from pyphot import msgs
from pyphot import utils
from pyphot import io
from pyphot.lacosmic import lacosmic
from pyphot.satdet import satdet
from pyphot.photometry import BKG2D, mask_bright_star

def ccdproc(scifiles, camera, det, n_process=4, science_path=None, masterbiasimg=None, masterdarkimg=None, masterpixflatimg=None,
            masterillumflatimg=None, bpm_proc=None, mask_vig=False, minimum_vig=0.5, apply_gain=False, grow=1.5,
            replace=None, sextractor_task='sex', verbose=True):

    n_file = len(scifiles)
    n_cpu = multiprocessing.cpu_count()

    if n_process > n_cpu:
        n_process = n_cpu

    if n_process>n_file:
        n_process = n_file
    sci_fits_list = []
    flag_fits_list = []

    if n_process == 1:
        for ii, scifile in enumerate(scifiles):
            sci_fits_file, flag_fits_file = _ccdproc_one(scifile, camera, det,
                        science_path=science_path, masterbiasimg=masterbiasimg, masterdarkimg=masterdarkimg,
                        masterpixflatimg=masterpixflatimg, masterillumflatimg=masterillumflatimg, bpm_proc=bpm_proc,
                        mask_vig=mask_vig, minimum_vig=minimum_vig, apply_gain=apply_gain, grow=grow,
                        replace=replace, sextractor_task=sextractor_task, verbose=verbose)
            sci_fits_list.append(sci_fits_file)
            flag_fits_list.append(flag_fits_file)
    else:
        msgs.info('Start parallel processing with n_process={:}'.format(n_process))
        work_queue = Queue()
        done_queue = Queue()
        processes = []

        for ii in range(n_file):
            work_queue.put((scifiles[ii], camera, det))

        # creating processes
        for w in range(n_process):
            p = Process(target=_ccdproc_worker, args=(work_queue, done_queue), kwargs={
                'science_path': science_path, 'masterbiasimg': masterbiasimg,
                'masterdarkimg': masterdarkimg, 'masterpixflatimg': masterpixflatimg, 'masterillumflatimg': masterillumflatimg,
                'bpm_proc': bpm_proc, 'mask_vig': mask_vig, 'minimum_vig': minimum_vig, 'apply_gain': apply_gain,
                'grow': grow, 'replace': replace, 'sextractor_task': sextractor_task, 'verbose':False})
            processes.append(p)
            p.start()

        # completing process
        for p in processes:
            p.join()

        # print the output
        while not done_queue.empty():
            sci_fits_file, flag_fits_file = done_queue.get()
            sci_fits_list.append(sci_fits_file)
            flag_fits_list.append(flag_fits_file)

    return sci_fits_list, flag_fits_list

def _ccdproc_one(scifile, camera, det, science_path=None, masterbiasimg=None, masterdarkimg=None, masterpixflatimg=None,
                 masterillumflatimg=None, bpm_proc=None, mask_vig=False, minimum_vig=0.5, apply_gain=False, grow=1.5,
                 replace=None, sextractor_task='sex', verbose=True):

    rootname = scifile.split('/')[-1]
    if science_path is not None:
        rootname = os.path.join(science_path,rootname)
        if '.gz' in rootname:
            rootname = rootname.replace('.gz','')
        elif '.fz' in rootname:
            rootname = rootname.replace('.fz','')

    # prepare output file names
    sci_fits_file = rootname.replace('.fits','_det{:02d}_proc.fits'.format(det))
    flag_fits_file = rootname.replace('.fits','_det{:02d}_ccdmask.fits'.format(det))

    if os.path.exists(sci_fits_file):
        msgs.info('The Science product {:} exists, skipping...'.format(sci_fits_file))
    else:
        msgs.info('Processing {:}'.format(scifile))
        detector_par, raw, header, exptime, rawdatasec_img, rawoscansec_img = camera.get_rawimage(scifile, det)
        saturation, nonlinear = detector_par['saturation'], detector_par['nonlinear']
        sci_image = trim_frame(raw, rawdatasec_img < 0.1)
        datasec_img = trim_frame(rawdatasec_img, rawdatasec_img < 0.1)
        oscansec_img = trim_frame(rawoscansec_img, rawdatasec_img < 0.1)

        # detector bad pixel mask
        bpm = camera.bpm(scifile, det, shape=None, msbias=None)>0.

        # Saturated pixel mask
        bpm_sat = sci_image > saturation*nonlinear

        # Zero pixel mask
        bpm_zero = sci_image == 0.

        # CCDPROC
        if masterbiasimg is not None:
            sci_image -= masterbiasimg
        if masterdarkimg is not None:
            sci_image -= masterdarkimg*exptime
        if masterpixflatimg is not None:
            sci_image *= utils.inverse(masterpixflatimg)
        if masterillumflatimg is not None:
            sci_image *= utils.inverse(masterillumflatimg)

        if apply_gain:
            numamplifiers = detector_par['numamplifiers']
            gain = detector_par['gain']
            for iamp in range(numamplifiers):
                this_amp = datasec_img == iamp+1
                sci_image[this_amp] = sci_image[this_amp] * gain[iamp]
            header['GAIN'] = (1.0, 'Effective gain')

        if bpm_proc is None:
            bpm_proc = np.zeros_like(sci_image, dtype='bool')

        # mask Vignetting pixels
        if (masterillumflatimg is not None) and (np.sum(masterillumflatimg!=1)>0):
            flat_for_vig = masterillumflatimg.copy()
        elif (masterpixflatimg is not None) and (np.sum(masterpixflatimg!=1)>0):
            flat_for_vig = masterpixflatimg.copy()
        else:
            flat_for_vig = np.ones_like(sci_image)
        if mask_vig:
            if verbose:
                msgs.info('Masking significantly vignetting (>{:}%) pixels'.format(minimum_vig*100))
            bpm_vig_1 = flat_for_vig < 1-minimum_vig

            # ToDo: Not sure whether the following is safe or not.
            #  Basically, we are using the sky background for vignetting.
            #  IMACS need this since the guider moves around the detector.
            bpm_for_vig = bpm | bpm_zero | bpm_vig_1 | bpm_proc
            starmask = mask_bright_star(sci_image, mask=bpm_for_vig, brightstar_nsigma=5., back_nsigma=3.,
                                        back_maxiters=5, method='sextractor', task=sextractor_task, verbose=verbose)
            bkg_for_vig, _ = BKG2D(sci_image, (50,50), mask=bpm_for_vig | starmask, filter_size=(3,3),
                                   sigclip=5., back_type='sextractor', verbose=verbose)
            bpm_vig_2 = sci_image < (1-minimum_vig) * np.median(bkg_for_vig[np.invert(bpm_for_vig)])
            bpm_vig_3 = bkg_for_vig < (1-minimum_vig) * np.median(bkg_for_vig[np.invert(bpm_for_vig)])
            bpm_vig_all = bpm_vig_1 | bpm_vig_2 | bpm_vig_3

            bpm_vig = grow_masked(bpm_vig_all, grow, verbose=verbose)
            ## Set viginetting pixel to be zero
            # sci_image[bpm_vig] = 0. # should do this in sci_proc, i.e. after the determination of sky background
        else:
            bpm_vig = np.zeros_like(sci_image, dtype=bool)

        # mask nan values
        bpm_nan = np.isnan(sci_image) | np.isinf(sci_image)
        sci_image[bpm_nan] = 0.

        '''
        ## master BPM mask, contains all bpm except for saturated values.
        bpm_all = bpm | bpm_zero | bpm_vig | bpm_nan | bpm_proc

        ## replace saturated values
        ## ToDo: explore the replacement algorithm, replace a bad pixel using the median of a box
        ##       replace saturated values with 65535 or maximum value, this will make the final coadd image looks better
        ##       replace other bad pixels with median or zero for other pixels?
        #sci_image[bpm_sat] = np.max(sci_image[np.invert(bpm_all)])

        ## replace other bad pixel values.
        if replace == 'zero':
            sci_image[bpm_all] = 0
        elif replace == 'median':
            _,sci_image[bpm_all],_ = stats.sigma_clipped_stats(sci_image, bpm_all, sigma=3, maxiters=5)
        elif replace == 'mean':
            sci_image[bpm_all],_,_ = stats.sigma_clipped_stats(sci_image, bpm_all, sigma=3, maxiters=5)
        elif replace == 'min':
            sci_image[bpm_all] = np.min(sci_image[np.invert(bpm_all)])
        elif replace == 'max':
            sci_image[bpm_all] = np.max(sci_image[np.invert(bpm_all)])
        else:
            if verbose:
                msgs.info('Not replacing bad pixel values')
        '''

        header['CCDPROC'] = ('TRUE', 'CCDPROC is done?')
        # save images
        io.save_fits(sci_fits_file, sci_image, header, 'ScienceImage', overwrite=True)
        msgs.info('Science image {:} saved'.format(sci_fits_file))
        flag_image = bpm*np.int(2**0) + bpm_proc*np.int(2**1) + bpm_sat*np.int(2**2) + \
                     bpm_zero * np.int(2**3) + bpm_nan*np.int(2**4) + bpm_vig*np.int(2**5)
        io.save_fits(flag_fits_file, flag_image.astype('int32'), header, 'FlagImage', overwrite=True)
        msgs.info('Flag image {:} saved'.format(flag_fits_file))

    return sci_fits_file, flag_fits_file

def _ccdproc_worker(work_queue, done_queue, science_path=None, masterbiasimg=None, masterdarkimg=None, masterpixflatimg=None,
                    masterillumflatimg=None, bpm_proc=None, mask_vig=False, minimum_vig=0.5, apply_gain=False, grow=1.5,
                    replace=None, sextractor_task='sex', verbose=True):

    """Multiprocessing worker for sciproc."""
    while not work_queue.empty():
        scifile, camera, det = work_queue.get()
        sci_fits_file, flag_fits_file = _ccdproc_one(scifile, camera, det,
                        science_path=science_path, masterbiasimg=masterbiasimg, masterdarkimg=masterdarkimg,
                        masterpixflatimg=masterpixflatimg, masterillumflatimg=masterillumflatimg,
                        bpm_proc=bpm_proc, mask_vig=mask_vig, minimum_vig=minimum_vig,
                        apply_gain=apply_gain, grow=grow, replace=replace,
                        sextractor_task=sextractor_task, verbose=verbose)

        done_queue.put((sci_fits_file, flag_fits_file))

def sciproc(scifiles, flagfiles, n_process=4, airmass=None, coeff_airmass=0., mastersuperskyimg=None,
            back_type='median', back_rms_type='std', back_size=(200,200), back_filtersize=(3, 3), back_maxiters=5, grow=1.5,
            maskbrightstar=True, brightstar_nsigma=3, maskbrightstar_method='sextractor', sextractor_task='sex',
            mask_cr=True, contrast=2, lamaxiter=1, sigclip=5.0, cr_threshold=5.0, neighbor_threshold=2.0,
            mask_sat=True, sat_sig=3.0, sat_buf=20, sat_order=3, low_thresh=0.1, h_thresh=0.5,
            small_edge=60, line_len=200, line_gap=75, percentile=(4.5, 93.0),
            mask_negative_star=False, replace=None, verbose=True):

    n_file = len(scifiles)
    n_cpu = multiprocessing.cpu_count()

    if n_process > n_cpu:
        n_process = n_cpu

    if n_process>n_file:
        n_process = n_file
    sci_fits_list = []
    wht_fits_list = []
    flag_fits_list = []

    if airmass is not None:
        if len(airmass) != len(scifiles):
            msgs.error('The length of airmass table should be the same with the number of exposures.')
    else:
        airmass = [None]*n_file
    if n_process == 1:
        for ii, scifile in enumerate(scifiles):
            sci_fits_file, wht_fits_file, flag_fits_file = _sciproc_one(scifile, flagfiles[ii],
                airmass=airmass[ii], coeff_airmass=coeff_airmass, mastersuperskyimg=mastersuperskyimg,
                back_type=back_type, back_rms_type=back_rms_type, back_size=back_size, back_filtersize=back_filtersize,
                back_maxiters=back_maxiters, grow=grow, maskbrightstar=maskbrightstar, brightstar_nsigma=brightstar_nsigma,
                maskbrightstar_method=maskbrightstar_method, sextractor_task=sextractor_task,
                mask_cr=mask_cr, contrast=contrast, lamaxiter=lamaxiter, sigclip=sigclip, cr_threshold=cr_threshold,
                neighbor_threshold=neighbor_threshold, mask_sat=mask_sat, sat_sig=sat_sig, sat_buf=sat_buf,
                sat_order=sat_order, low_thresh=low_thresh, h_thresh=h_thresh,
                small_edge=small_edge, line_len=line_len, line_gap=line_gap, percentile=percentile,
                mask_negative_star=mask_negative_star, replace=replace, verbose=verbose)
            sci_fits_list.append(sci_fits_file)
            wht_fits_list.append(wht_fits_file)
            flag_fits_list.append(flag_fits_file)
    else:
        msgs.info('Start parallel processing with n_process={:}'.format(n_process))
        work_queue = Queue()
        done_queue = Queue()
        processes = []

        for ii in range(n_file):
            work_queue.put((scifiles[ii], flagfiles[ii], airmass[ii]))

        # creating processes
        for w in range(n_process):
            p = Process(target=_sciproc_worker, args=(work_queue, done_queue), kwargs={
                'mastersuperskyimg': mastersuperskyimg, 'coeff_airmass': coeff_airmass,
                'back_type': back_type, 'back_rms_type': back_rms_type, 'back_size': back_size, 'back_filtersize': back_filtersize,
                'back_maxiters': back_maxiters, 'grow': grow, 'maskbrightstar': maskbrightstar, 'brightstar_nsigma': brightstar_nsigma,
                'maskbrightstar_method': maskbrightstar_method, 'sextractor_task': sextractor_task,
                'mask_cr': mask_cr, 'contrast': contrast, 'lamaxiter': lamaxiter, 'sigclip': sigclip, 'cr_threshold': cr_threshold,
                'neighbor_threshold': neighbor_threshold, 'mask_sat': mask_sat, 'sat_sig': sat_sig, 'sat_buf': sat_buf,
                'sat_order': sat_order, 'low_thresh': low_thresh, 'h_thresh': h_thresh,
                'small_edge': small_edge, 'line_len': line_len, 'line_gap': line_gap, 'percentile': percentile,
                'mask_negative_star': mask_negative_star, 'replace': replace, 'verbose':False})
            processes.append(p)
            p.start()

        # completing process
        for p in processes:
            p.join()

        # print the output
        while not done_queue.empty():
            sci_fits_file, wht_fits_file, flag_fits_file = done_queue.get()
            sci_fits_list.append(sci_fits_file)
            wht_fits_list.append(wht_fits_file)
            flag_fits_list.append(flag_fits_file)

    return sci_fits_list, wht_fits_list, flag_fits_list

def _sciproc_one(scifile, flagfile, airmass, coeff_airmass=0., mastersuperskyimg=None,
                 back_type='median', back_rms_type='std', back_size=(200,200), back_filtersize=(3, 3), back_maxiters=5, grow=1.5,
                 maskbrightstar=True, brightstar_nsigma=3, maskbrightstar_method='sextractor', sextractor_task='sex',
                 mask_cr=True, contrast=2, lamaxiter=1, sigclip=5.0, cr_threshold=5.0, neighbor_threshold=2.0,
                 mask_sat=True, sat_sig=3.0, sat_buf=20, sat_order=3, low_thresh=0.1, h_thresh=0.5,
                 small_edge=60, line_len=200, line_gap=75, percentile=(4.5, 93.0),
                 mask_negative_star=False, replace=None, verbose=True):

    # prepare output names
    sci_fits_file = scifile.replace('_proc.fits','_sci.fits')
    wht_fits_file = scifile.replace('_proc.fits','_sci.weight.fits')
    flag_fits_file = scifile.replace('_proc.fits','_flag.fits')

    if os.path.exists(sci_fits_file):
        msgs.info('The Science product {:} exists, skipping...'.format(sci_fits_file))
    else:
        msgs.info('Processing {:}'.format(scifile))
        header, data, _ = io.load_fits(scifile)
        _, flag_image, _ = io.load_fits(flagfile)
        bpm = flag_image>0
        bpm_zero = (data == 0.)
        bpm_saturation = (flag_image & 2**2)>0
        bpm_vig = (flag_image & 2**5)>0
        bpm_vig_only = np.logical_and(bpm_vig, np.invert(bpm_zero)) # pixels identified by vig but not zeros

        ## super flattening your images
        if mastersuperskyimg is not None:
            data *= utils.inverse(mastersuperskyimg)

        # do the extinction correction.
        if airmass is not None:
            mag_ext = coeff_airmass * (airmass-1)
            data *= 10**(0.4*mag_ext)

        # mask bright stars before estimating the background
        # do not mask viginetting pixels when estimating the background to reduce edge effect
        bpm_for_star = np.logical_and(bpm, np.invert(bpm_vig_only))
        if maskbrightstar:
            starmask = mask_bright_star(data, mask=bpm_for_star, brightstar_nsigma=brightstar_nsigma, back_nsigma=sigclip,
                                        back_maxiters=back_maxiters, method=maskbrightstar_method, task=sextractor_task,
                                        verbose=verbose)
            starmask = grow_masked(starmask, grow, verbose=verbose)
        else:
            starmask = np.zeros_like(data, dtype=bool)

        # estimate the 2D background with all masks
        bpm_for_bkg = np.logical_or(bpm, starmask)
        background_array, background_rms = BKG2D(data, back_size, mask=bpm_for_bkg, filter_size=back_filtersize,
                                                 sigclip=sigclip, back_type=back_type, back_rms_type=back_rms_type,
                                                 back_maxiters=back_maxiters,sextractor_task=sextractor_task,
                                                 verbose=verbose)
        background_array[bpm_zero] = 0.

        # subtract the background
        if verbose:
            msgs.info('Subtracting 2D background')
        sci_image = data-background_array

        # CR mask
        # do not trade saturation as bad pixel when searching for CR and satellite trail
        bpm_for_cr = np.logical_and(bpm, np.invert(bpm_saturation))
        if mask_cr:
            if verbose:
                msgs.info('Identifying cosmic rays using the L.A.Cosmic algorithm')
            bpm_cr_tmp = lacosmic(sci_image, contrast, cr_threshold, neighbor_threshold,
                                  error=background_rms, mask=bpm_for_cr, background=background_array, effective_gain=None,
                                  readnoise=None, maxiter=lamaxiter, border_mode='mirror', verbose=verbose)
            bpm_cr = grow_masked(bpm_cr_tmp, grow, verbose=verbose)
        else:
            if verbose:
                msgs.warn('Skipped cosmic ray rejection process!')
            bpm_cr = np.zeros_like(sci_image,dtype=bool)

        # satellite trail mask
        if mask_sat:
            if verbose:
                msgs.info('Identifying satellite trails using the Canny algorithm following ACSTOOLS.')
            bpm_sat_tmp = satdet(sci_image, bpm=bpm_for_cr, sigma=sat_sig, buf=sat_buf, order=sat_order,
                                 low_thresh=low_thresh, h_thresh=h_thresh, small_edge=small_edge,
                                 line_len=line_len, line_gap=line_gap, percentile=percentile, verbose=verbose)
            bpm_sat = grow_masked(bpm_sat_tmp, grow, verbose=verbose)
        else:
            bpm_sat = np.zeros_like(sci_image,dtype=bool)

        # negative star mask
        if mask_negative_star:
            if verbose:
                msgs.info('Masking negative stars with {:}'.format(maskbrightstar_method))
            bpm_negative_tmp = mask_bright_star(0-sci_image, mask=bpm, brightstar_nsigma=brightstar_nsigma, back_nsigma=sigclip,
                                                back_maxiters=back_maxiters, method=maskbrightstar_method, task=sextractor_task,
                                                verbose=verbose)
            bpm_negative = grow_masked(bpm_negative_tmp, grow, verbose=verbose)
        else:
            bpm_negative = np.zeros_like(sci_image, dtype=bool)

        # add the cosmic ray and satellite trail flag to the flag images
        flag_image_new = flag_image + bpm_cr.astype('int32')*np.int(2**6) + bpm_sat.astype('int32')*np.int(2**7)
        flag_image_new += bpm_negative.astype('int32')*np.int(2**8)

        ## replace bad pixels but not saturated pixels to make the image nicer
        #flag_image = bpm*np.int(2**0) + bpm_proc*np.int(2**1) + bpm_sat*np.int(2**2) + \
        #             bpm_zero * np.int(2**3) + bpm_nan*np.int(2**4) + bpm_vig*np.int(2**5)
        # ToDo: explore the replacement algorithm, replace a bad pixel using the median of a box
        bpm_all = flag_image_new>0
        bpm_replace = np.logical_and(bpm_all, np.invert(bpm_saturation))
        if replace == 'zero':
            sci_image[bpm_replace] = 0
        elif replace == 'median':
            _,sci_image[bpm_replace],_ = stats.sigma_clipped_stats(sci_image, bpm_all, sigma=sigclip, maxiters=5)
        elif replace == 'mean':
            sci_image[bpm_replace],_,_ = stats.sigma_clipped_stats(sci_image, bpm_all, sigma=sigclip, maxiters=5)
        elif replace == 'min':
            sci_image[bpm_replace] = np.min(sci_image[np.invert(bpm_all)])
        elif replace == 'max':
            sci_image[bpm_replace] = np.max(sci_image[np.invert(bpm_all)])
        else:
            if verbose:
                msgs.info('Not replacing bad pixel values')

        # Generate weight map used for SExtractor and SWarp (WEIGHT_TYPE = MAP_WEIGHT)
        # ToDo: use 1/var as the weight map. The var can either be estimated from RN/photon/bkg or from BKG2D.
        wht_image = utils.inverse(background_array)

        # Set bad pixel's weight to be zero
        wht_image[flag_image_new>0] = 0

        # save images
        io.save_fits(sci_fits_file, sci_image, header, 'ScienceImage', overwrite=True)
        msgs.info('Science image {:} saved'.format(sci_fits_file))
        io.save_fits(wht_fits_file, wht_image, header, 'WeightImage', overwrite=True)
        msgs.info('Weight image {:} saved'.format(wht_fits_file))
        io.save_fits(flag_fits_file, flag_image_new.astype('int32'), header, 'FlagImage', overwrite=True)
        msgs.info('Flag image {:} saved'.format(flag_fits_file))

    return sci_fits_file, wht_fits_file, flag_fits_file

def _sciproc_worker(work_queue, done_queue, coeff_airmass=0., mastersuperskyimg=None,
                    back_type='median', back_rms_type='std', back_size=(200,200), back_filtersize=(3, 3), back_maxiters=5, grow=1.5,
                    maskbrightstar=True, brightstar_nsigma=3, maskbrightstar_method='sextractor', sextractor_task='sex',
                    mask_cr=True, contrast=2, lamaxiter=1, sigclip=5.0, cr_threshold=5.0, neighbor_threshold=2.0,
                    mask_sat=True, sat_sig=3.0, sat_buf=20, sat_order=3, low_thresh=0.1, h_thresh=0.5,
                    small_edge=60, line_len=200, line_gap=75, percentile=(4.5, 93.0),
                    mask_negative_star=False, replace=None, verbose=False):

    """Multiprocessing worker for sciproc."""
    while not work_queue.empty():
        scifile, flagfile, airmass = work_queue.get()
        sci_fits_file, wht_fits_file, flag_fits_file = _sciproc_one(scifile, flagfile, airmass,
            coeff_airmass=coeff_airmass, mastersuperskyimg=mastersuperskyimg,
            back_type=back_type, back_rms_type=back_rms_type, back_size=back_size, back_filtersize=back_filtersize,
            back_maxiters=back_maxiters, grow=grow, maskbrightstar=maskbrightstar, brightstar_nsigma=brightstar_nsigma,
            maskbrightstar_method=maskbrightstar_method, sextractor_task=sextractor_task,
            mask_cr=mask_cr, contrast=contrast, lamaxiter=lamaxiter, sigclip=sigclip, cr_threshold=cr_threshold,
            neighbor_threshold=neighbor_threshold, mask_sat=mask_sat, sat_sig=sat_sig, sat_buf=sat_buf,
            sat_order=sat_order, low_thresh=low_thresh, h_thresh=h_thresh,
            small_edge=small_edge, line_len=line_len, line_gap=line_gap, percentile=percentile,
            mask_negative_star=mask_negative_star, replace=replace, verbose=verbose)

        done_queue.put((sci_fits_file, wht_fits_file, flag_fits_file))

def gain_frame(amp_img, gain):
    """
    Generate an image with the gain for each pixel.

    Args:
        amp_img (`numpy.ndarray`_):
            Integer array that identifies which (1-indexed) amplifier
            was used to read each pixel.
        gain (:obj:`list`):
            List of amplifier gain values.  Must be that the gain for
            amplifier 1 is provided by `gain[0]`, etc.

    Returns:
        `numpy.ndarray`_: Image with the gain for each pixel.
    """
    # TODO: Remove this or actually do it.
    # msgs.warn("Should probably be measuring the gain across the amplifier boundary")

    # Build the gain image
    gain_img = np.zeros_like(amp_img, dtype=float)
    for i,_gain in enumerate(gain):
        gain_img[amp_img == i+1] = _gain

    # Return the image, trimming if requested
    return gain_img

def trim_frame(frame, mask):
    """
    Trim the masked regions from a frame.

    Args:
        frame (:obj:`numpy.ndarray`):
            Image to be trimmed
        mask (:obj:`numpy.ndarray`):
            Boolean image set to True for values that should be trimmed
            and False for values to be returned in the output trimmed
            image.

    Return:
        :obj:`numpy.ndarray`:
            Trimmed image

    Raises:
        PypPhotError:
            Error raised if the trimmed image includes masked values
            because the shape of the valid region is odd.
    """
    if np.any(mask[np.invert(np.all(mask,axis=1)),:][:,np.invert(np.all(mask,axis=0))]):
        msgs.error('Data section is oddly shaped.  Trimming does not exclude all '
                   'pixels outside the data sections.')
    return frame[np.invert(np.all(mask,axis=1)),:][:,np.invert(np.all(mask,axis=0))]

def grow_masked(img, grow, verbose=True):

    img = img.astype(float)
    growval =1.0
    if verbose:
        msgs.info('Growing mask by {:}'.format(grow))
    if not np.any(img == growval):
        return img.astype(bool)

    _img = img.copy()
    sz_x, sz_y = img.shape
    d = int(1+grow)
    rsqr = grow*grow

    # Grow any masked values by the specified amount
    for x in range(sz_x):
        for y in range(sz_y):
            if img[x,y] != growval:
                continue

            mnx = 0 if x-d < 0 else x-d
            mxx = x+d+1 if x+d+1 < sz_x else sz_x
            mny = 0 if y-d < 0 else y-d
            mxy = y+d+1 if y+d+1 < sz_y else sz_y

            for i in range(mnx,mxx):
                for j in range(mny, mxy):
                    if (i-x)*(i-x)+(j-y)*(j-y) <= rsqr:
                        _img[i,j] = growval
    return _img.astype(bool)

'''
The following functions are old codes that do not support parallel processsing. 
'''

def ccdproc_old(scifiles, camera, det, science_path=None, masterbiasimg=None, masterdarkimg=None, masterpixflatimg=None,
                masterillumflatimg=None, bpm_proc=None, mask_vig=False, minimum_vig=0.5, apply_gain=False, grow=1.5,
                replace=None, sextractor_task='sex', verbose=True):

    sci_fits_list = []
    flag_fits_list = []
    for ifile in scifiles:
        #ToDo: parallel this
        rootname = ifile.split('/')[-1]
        if science_path is not None:
            rootname = os.path.join(science_path,rootname)
            if '.gz' in rootname:
                rootname = rootname.replace('.gz','')
            elif '.fz' in rootname:
                rootname = rootname.replace('.fz','')

        # prepare output file names
        sci_fits = rootname.replace('.fits','_det{:02d}_proc.fits'.format(det))
        sci_fits_list.append(sci_fits)
        flag_fits = rootname.replace('.fits','_det{:02d}_ccdmask.fits'.format(det))
        flag_fits_list.append(flag_fits)

        if os.path.exists(sci_fits):
            msgs.info('The Science product {:} exists, skipping...'.format(sci_fits))
        else:
            msgs.info('Processing {:}'.format(ifile))
            detector_par, sci_image, header, exptime, gain_image, rn_image = camera.get_rawimage(ifile, det)
            saturation, nonlinear = detector_par['saturation'], detector_par['nonlinear']
            #darkcurr = detector_par['det{:02d}'.format(det)]['darkcurr']

            # detector bad pixel mask
            bpm = camera.bpm(ifile, det, shape=None, msbias=None)>0.

            # Saturated pixel mask
            bpm_sat = sci_image > saturation*nonlinear

            # Zero pixel mask
            bpm_zero = sci_image == 0.

            # CCDPROC
            if masterbiasimg is not None:
                sci_image -= masterbiasimg
            if masterdarkimg is not None:
                sci_image -= masterdarkimg*exptime
            if masterpixflatimg is not None:
                sci_image *= utils.inverse(masterpixflatimg)
            if masterillumflatimg is not None:
                sci_image *= utils.inverse(masterillumflatimg)
            if apply_gain:
                header['GAIN'] = (1.0, 'Effective gain')
                sci_image *= gain_image

            if bpm_proc is None:
                bpm_proc = np.zeros_like(sci_image, dtype='bool')

            # mask Vignetting pixels
            if masterillumflatimg is not None:
                flat_for_vig = masterillumflatimg.copy()
            elif masterpixflatimg is not None:
                flat_for_vig = masterpixflatimg.copy()
            else:
                flat_for_vig = np.ones_like(sci_image)
            if mask_vig:
                msgs.info('Masking significantly vignetting (>{:}%) pixels'.format(minimum_vig*100))
                bpm_vig_1 = flat_for_vig < 1-minimum_vig

                # ToDo: Not sure whether the following is safe or not.
                #  Basically, we are using the sky background for vignetting.
                #  IMACS need this since the guider moves around the detector.
                bpm_for_vig = bpm | bpm_zero | bpm_vig_1 | bpm_proc
                starmask = mask_bright_star(sci_image, mask=bpm_for_vig, brightstar_nsigma=5., back_nsigma=3.,
                                            back_maxiters=5, method='sextractor', task=sextractor_task)
                bkg_for_vig, _ = BKG2D(sci_image, (50,50), mask=bpm_for_vig | starmask, filter_size=(3,3),
                                       sigclip=5., back_type='sextractor')
                bpm_vig_2 = sci_image < (1-minimum_vig) * np.median(bkg_for_vig[np.invert(bpm_for_vig)])
                bpm_vig_3 = bkg_for_vig < (1-minimum_vig) * np.median(bkg_for_vig[np.invert(bpm_for_vig)])
                bpm_vig_all = bpm_vig_1 | bpm_vig_2 | bpm_vig_3

                bpm_vig = grow_masked(bpm_vig_all, grow, verbose=verbose)
                ## Set viginetting pixel to be zero
                sci_image[bpm_vig] = 0.
            else:
                bpm_vig = np.zeros_like(sci_image, dtype=bool)

            #if mask_vig and (masterillumflatimg is not None):
            #    bpm_vig = masterillumflatimg<minimum_vig
            #elif mask_vig and (masterpixflatimg is not None):
            #    bpm_vig = masterpixflatimg < minimum_vig
            #else:
            #    bpm_vig = np.zeros_like(sci_image, dtype=bool)

            # mask nan values
            bpm_nan = np.isnan(sci_image) | np.isinf(sci_image)

            ## master BPM mask, contains all bpm except for saturated values.
            bpm_all = bpm | bpm_zero | bpm_vig | bpm_nan | bpm_proc

            ## replace saturated values
            ## ToDo: explore the replacement algorithm, replace a bad pixel using the median of a box
            ##       replace saturated values with 65535 or maximum value, this will make the final coadd image looks better
            ##       replace other bad pixels with median or zero for other pixels?
            #sci_image[bpm_sat] = np.max(sci_image[np.invert(bpm_all)])

            ## replace other bad pixel values.
            if replace == 'zero':
                sci_image[bpm_all] = 0
            elif replace == 'median':
                _,sci_image[bpm_all],_ = stats.sigma_clipped_stats(sci_image, bpm_all, sigma=3, maxiters=5)
            elif replace == 'mean':
                sci_image[bpm_all],_,_ = stats.sigma_clipped_stats(sci_image, bpm_all, sigma=3, maxiters=5)
            elif replace == 'min':
                sci_image[bpm_all] = np.min(sci_image[np.invert(bpm_all)])
            elif replace == 'max':
                sci_image[bpm_all] = np.max(sci_image[np.invert(bpm_all)])
            else:
                msgs.info('Not replacing bad pixel values')

            header['CCDPROC'] = ('TRUE', 'CCDPROC is done?')
            # save images
            io.save_fits(sci_fits, sci_image, header, 'ScienceImage', overwrite=True)
            msgs.info('Science image {:} saved'.format(sci_fits))
            flag_image = bpm*np.int(2**0) + bpm_proc*np.int(2**1) + bpm_sat*np.int(2**2) + \
                         bpm_zero * np.int(2**3) + bpm_vig*np.int(2**4) + bpm_nan*np.int(2**5)
            io.save_fits(flag_fits, flag_image.astype('int32'), header, 'FlagImage', overwrite=True)
            msgs.info('Flag image {:} saved'.format(flag_fits))

    return sci_fits_list, flag_fits_list

def sciproc_old(scifiles, flagfiles, mastersuperskyimg=None, airmass=None, coeff_airmass=0.,
            back_type='median', back_rms_type='std', back_size=(200,200), back_filtersize=(3, 3), back_maxiters=5, grow=1.5,
            maskbrightstar=True, brightstar_nsigma=3, maskbrightstar_method='sextractor', sextractor_task='sex',
            mask_cr=True, contrast=2, lamaxiter=1, sigclip=5.0, cr_threshold=5.0, neighbor_threshold=2.0,
            mask_sat=True, sat_sig=3.0, sat_buf=20, sat_order=3, low_thresh=0.1, h_thresh=0.5,
            small_edge=60, line_len=200, line_gap=75, percentile=(4.5, 93.0),
            mask_negative_star=False, replace=None):

    sci_fits_list = []
    wht_fits_list = []
    flag_fits_list = []
    for ii, ifile in enumerate(scifiles):
        #ToDo: parallel this
        # prepare output file names
        sci_fits = ifile.replace('_proc.fits','_sci.fits')
        sci_fits_list.append(sci_fits)
        wht_fits = ifile.replace('_proc.fits','_sci.weight.fits')
        wht_fits_list.append(wht_fits)
        flag_fits = ifile.replace('_proc.fits','_flag.fits')
        flag_fits_list.append(flag_fits)
        if os.path.exists(sci_fits):
            msgs.info('The Science product {:} exists, skipping...'.format(sci_fits))
        else:
            msgs.info('Processing {:}'.format(ifile))
            header, data, _ = io.load_fits(ifile)
            _, flag_image, _ = io.load_fits(flagfiles[ii])
            bpm = flag_image>0
            bpm_zero = data == 0.

            ## super flattening your images
            if mastersuperskyimg is not None:
                data *= utils.inverse(mastersuperskyimg)

            if airmass is not None:
                if len(airmass) != len(scifiles):
                    msgs.error('The length of airmass table should be the same with the number of exposures.')

                # do the correction.
                mag_ext = coeff_airmass * (airmass[ii]-1)
                data *= 10**(0.4*mag_ext)

            # mask bright stars before estimating the background
            if maskbrightstar:
                starmask = mask_bright_star(data, mask=bpm, brightstar_nsigma=brightstar_nsigma, back_nsigma=sigclip,
                                            back_maxiters=back_maxiters, method=maskbrightstar_method, task=sextractor_task)
            else:
                starmask = np.zeros_like(data, dtype=bool)

            # estimate the 2D background with all masks
            # do not mask viginetting pixels when estimating the background to reduce edge effect
            bpm_bkg = (bpm | starmask)
            background_array, background_rms = BKG2D(data, back_size, mask=bpm_bkg, filter_size=back_filtersize,
                                                     sigclip=sigclip, back_type=back_type, back_rms_type=back_rms_type,
                                                     back_maxiters=back_maxiters,sextractor_task=sextractor_task)
            ## OLD Sky background subtraction
            # ToDo: the following seems having memory leaking, need to solve the issue or switch to SExtractor.
            #from astropy.stats import SigmaClip
            #from photutils import Background2D
            #from photutils import MeanBackground, MedianBackground, SExtractorBackground
            #sigma_clip = SigmaClip(sigma=sigclip)
            #if back_type == 'median':
            #    bkg_estimator = MedianBackground()
            #elif back_type == 'mean':
            #    bkg_estimator = MeanBackground()
            #else:
            #    bkg_estimator = SExtractorBackground()
            #tmp = data.copy()
            #bkg = Background2D(tmp, back_size, mask=mask_bkg, filter_size=back_filtersize,
            #                   sigma_clip=sigma_clip, bkg_estimator=bkg_estimator)
            #background_array = bkg.background
            #background_rms = bkg.background_rms
            # clean up memory
            #del tmp, bkg, data, mask_bkg
            #gc.collect()

            # subtract the background
            msgs.info('Subtracting 2D background')
            sci_image = data-background_array

            # CR mask
            if mask_cr:
                msgs.info('Identifying cosmic rays using the L.A.Cosmic algorithm')
                bpm_cr_tmp = lacosmic(sci_image, contrast, cr_threshold, neighbor_threshold,
                                      error=background_rms, mask=bpm, background=background_array, effective_gain=None,
                                      readnoise=None, maxiter=lamaxiter, border_mode='mirror')
                bpm_cr = grow_masked(bpm_cr_tmp, grow, verbose=verbose)
                # seems not working as good as lacosmic.py
                # grow=1.5, remove_compact_obj=True, sigfrac=0.3, objlim=5.0,
                #bpm_cr = lacosmic_pypeit(sci_image, saturation, nonlinear, varframe=None, maxiter=maxiter, grow=grow,
                #                  remove_compact_obj=remove_compact_obj, sigclip=sigclip, sigfrac=sigfrac, objlim=objlim)
            else:
                msgs.warn('Skipped cosmic ray rejection process!')
                bpm_cr = np.zeros_like(sci_image,dtype=bool)

            # satellite trail mask
            if mask_sat:
                msgs.info('Identifying satellite trails using the Canny algorithm following ACSTOOLS.')
                bpm_sat = satdet(sci_image, bpm=bpm|bpm_cr, sigma=sat_sig, buf=sat_buf, order=sat_order,
                                 low_thresh=low_thresh, h_thresh=h_thresh, small_edge=small_edge,
                                 line_len=line_len, line_gap=line_gap, percentile=percentile)
            else:
                bpm_sat = np.zeros_like(sci_image,dtype=bool)

            # negative star mask
            if mask_negative_star:
                msgs.info('Masking negative stars with {:}'.format(maskbrightstar_method))
                bpm_negative_tmp = mask_bright_star(0-sci_image, mask=bpm, brightstar_nsigma=brightstar_nsigma, back_nsigma=sigclip,
                                                back_maxiters=back_maxiters, method=maskbrightstar_method, task=sextractor_task)
                bpm_negative = grow_masked(bpm_negative_tmp, grow, verbose=verbose)
            else:
                bpm_negative = np.zeros_like(sci_image, dtype=bool)

            # add the cosmic ray and satellite trail flag
            flag_image_new = flag_image + bpm_cr.astype('int32')*np.int(2**6) + bpm_sat.astype('int32')*np.int(2**7)
            flag_image_new += bpm_negative.astype('int32')*np.int(2**8)

            # make a mask used for statistics.
            # should not include starmask since they are not bad pixels if you want to use this mask for other purpose
            mask_all = bpm | bpm_cr | bpm_sat | bpm_negative | starmask

            ## replace cosmic ray and satellite affected pixels?
            # ToDo: explore the replacement algorithm, replace a bad pixel using the median of a box
            bpm_replace = bpm_cr | bpm_sat
            if replace == 'zero':
                sci_image[bpm_replace] = 0
            elif replace == 'median':
                _,sci_image[bpm_replace],_ = stats.sigma_clipped_stats(sci_image, mask_all, sigma=sigclip, maxiters=5)
            elif replace == 'mean':
                sci_image[bpm_replace],_,_ = stats.sigma_clipped_stats(sci_image, mask_all, sigma=sigclip, maxiters=5)
            elif replace == 'min':
                sci_image[bpm_replace] = np.min(sci_image[np.invert(mask_all)])
            elif replace == 'max':
                sci_image[bpm_replace] = np.max(sci_image[np.invert(mask_all)])
            else:
                msgs.info('Not replacing bad pixel values')

            # Generate weight map used for SExtractor and SWarp (WEIGHT_TYPE = MAP_WEIGHT)
            wht_image = utils.inverse(background_array)

            # Always set original zero values to be zero, this can avoid significant negative values after sky subtraction
            sci_image[bpm_zero] = 0
            # Also set negative stars to be zero
            sci_image[bpm_negative] = 0

            # Set bad pixel's weight to be zero
            wht_image[flag_image_new>0] = 0

            # save images
            io.save_fits(sci_fits, sci_image, header, 'ScienceImage', overwrite=True)
            msgs.info('Science image {:} saved'.format(sci_fits))
            io.save_fits(wht_fits, wht_image, header, 'WeightImage', overwrite=True)
            msgs.info('Weight image {:} saved'.format(wht_fits))
            io.save_fits(flag_fits, flag_image_new.astype('int32'), header, 'FlagImage', overwrite=True)
            msgs.info('Flag image {:} saved'.format(flag_fits))

    return sci_fits_list, wht_fits_list, flag_fits_list
