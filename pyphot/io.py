import sys, os, gc
import time
import scipy
import astropy

import numpy as np
from astropy.io import fits
from astropy.table import Table

from pkg_resources import resource_filename

import pyphot
from pyphot import msgs

def initialize_header(hdr=None, primary=False):
    """
    Initialize a FITS header.

    Args:
        hdr (`astropy.io.fits.Header`, optional):
            Header object to update with basic summary
            information. The object is modified in-place and also
            returned. If None, an empty header is instantiated,
            edited, and returned.
        primary (bool, optional):
            If true and hdr is None, generate a Primary header

    Returns:
        `astropy.io.fits.Header`: The initialized (or edited)
        fits header.
    """
    # Add versioning; this hits the highlights but should it add
    # the versions of all packages included in the requirements.txt
    # file?
    if hdr is None:
        if primary:
            hdr = fits.PrimaryHDU().header
        else:
            hdr = fits.Header()
    hdr['VERSPYT'] = ('.'.join([ str(v) for v in sys.version_info[:3]]), 'Python version')
    hdr['VERSNPY'] = (np.__version__, 'Numpy version')
    hdr['VERSSCI'] = (scipy.__version__, 'Scipy version')
    hdr['VERSAST'] = (astropy.__version__, 'Astropy version')
    hdr['VERSPYP'] = (pyphot.__version__, 'PyPhot version')

    # Save the date of the reduction
    hdr['DATE'] = (time.strftime('%Y-%m-%d',time.gmtime()), 'UTC date created')

    # TODO: Anything else?

    # Return
    return hdr

def save_fits(fitsname, data, header, img_type, mask=None, overwrite=True):

    if header.get('VERSPYP') is None:
        # Add some Header card
        hdr = initialize_header(hdr=None, primary=True)
        hdr['IMGTYP'] = (img_type, 'PyPhot image type')
        for i in range(len(hdr)):
            header.append(hdr.cards[i])

    if mask is None:
        hdu = fits.PrimaryHDU(data, header=header)
        hdu.writeto(fitsname, overwrite=overwrite)
    else:
        hdu = fits.PrimaryHDU(header=header)
        hdu1 = fits.ImageHDU(data, header=header, name='IMAGE')
        hdu2 = fits.ImageHDU(mask.astype('int32'), header=header, name='MASK')
        new_hdul = fits.HDUList([hdu, hdu1, hdu2])
        new_hdul.writeto(fitsname, overwrite=True)
        #mask_hdu = fits.ImageHDU(mask.astype('int32'), name='MASK')
        #hdulist = fits.HDUList([hdu,mask_hdu])
        #hdulist.writeto(fitsname,overwrite=overwrite)
        del new_hdul[1].data
        del new_hdul[2].data
        new_hdul.close()
        gc.collect()


def load_fits(fitsname):
    par = fits.open(fitsname, memmap=False)
    if len(par)==1:
        head, data, flag = par[0].header, par[0].data, np.zeros_like(par[0].data,dtype='int32')
        del par[0].data
    elif len(par)==3:
        head, data, flag = par[1].header, par[1].data, par[2].data
        del par[1].data
        del par[2].data
    else:
        msgs.error('{:} is not a PyPhot FITS Image.'.format(fitsname))
        return None
    par.close()
    gc.collect()

    return head, data, flag


def load_filter(filter):
    """
    Load a system response curve for a given filter

    Args:
        filter (str): Name of filter

    Returns:
        ndarray, ndarray: wavelength, instrument throughput

    """
    filter_file = resource_filename('pyphot', os.path.join('data', 'filters', 'filter_list.ascii'))
    tbl = Table.read(filter_file, format='ascii')
    allowed_options = tbl['filter'].data

    # Check
    if filter not in allowed_options:
        msgs.error("PyPhot is not ready for filter = {}".format(filter))

    trans_file = resource_filename('pyphot', os.path.join('data', 'filters', 'filtercurves.fits'))
    trans = fits.open(trans_file, memmap=False)
    wave = trans[filter].data['lam']  # Angstroms
    instr = trans[filter].data['Rlam']  # Am keeping in atmospheric terms
    keep = instr > 0.
    # Parse
    wave = wave[keep]
    instr = instr[keep]

    # Return
    return wave, instr
