import os,re,subprocess

from pyphot import msgs
from pkg_resources import resource_filename
config_dir = resource_filename('pyphot', '/config/')

def get_version():
    """
    To find the SCAMP version
    returns: a string (e.g. '2.4.4')
    """
    v = subprocess.Popen("scamp", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = v.communicate()
    version_match = re.search("[Vv]ersion ([0-9\.])+", err.decode("utf-8"))
    version = str(version_match.group()[8:])
    return version


def get_default_config(defaultconfig='pyphot', workdir='./'):
    """
    To get the default SCAMP configuration file
    """

    if defaultconfig == "scamp":
        p = subprocess.Popen(["scamp", "-d"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate()
        f = open(os.path.join(workdir, "config.scamp"), "w")
        f.write(out)
        f.close()
        msgs.info("config.scamp generated from Scamp default configuration")
    elif defaultconfig == "pyphot":
        os.system("cp " + os.path.join(config_dir,"scamp.config") + ' ' + os.path.join(workdir,"config.scamp"))
        msgs.info("config.scamp generated from PyPhot default configuration")
    else:
        os.system("cp " + defaultconfig + ' ' + os.path.join(workdir,"config.scamp"))
        msgs.info("Using user provided configuration for Scamp")

    comd = ["-c", os.path.join(workdir,"config.scamp")]
    return comd

def get_config(config=None, workdir="./"):
    """
    append some configure parameters to the default configure file
    e.g. config = {"FGROUP_RADIUS":1.0, "ASTREF_CATALOG":"SDSS-R9","CROSSID_RADIUS":2.0,"SOLVE_PHOTOM":"Y",
                     "MAGZERO_OUT":24.5,"ASTREFMAG_LIMITS":"10,30"}
    """
    if config is None:
        configapp = []
    else:
        configapp = []

        if "MERGEDOUTCAT_NAME" in config:
            config["MERGEDOUTCAT_NAME"] = os.path.join(workdir, config["MERGEDOUTCAT_NAME"])
        if "FULLOUTCAT_NAME" in config:
            config["FULLOUTCAT_NAME"] = os.path.join(workdir, config["FULLOUTCAT_NAME"])

        for (key, value) in config.items():
            configapp.append("-" + str(key))
            configapp.append(str(value).replace(' ', ''))

    return configapp


def scampone(imgname, config=None, workdir='./', QAdir='./', defaultconfig='pyphot', delete=True, log=False):

    ## Get the version of your SCAMP
    scampversion = get_version()
    msgs.info("Scamp version is {:}".format(scampversion))

    ## Generate the configuration file
    configcomd = get_default_config(defaultconfig=defaultconfig, workdir=workdir)

    ## append your configuration
    if 'CHECKPLOT_NAME' in config:
        checkplot_name = config['CHECKPLOT_NAME'].split(',')
        checkplot_name_new = []
        for iname in checkplot_name:
            tmp = os.path.join(QAdir,'{:}_{:}'.format(os.path.split(imgname)[1].replace('.fits',''),iname))
            checkplot_name_new.append(tmp.replace('.','_'))
        separator = ','
        config['CHECKPLOT_NAME'] = separator.join(checkplot_name_new)

    configapp = get_config(config=config)

    catname = imgname.replace('.fits','_cat.fits')
    comd = ["scamp"] + [os.path.join(workdir, catname)] + configcomd + configapp
    p = subprocess.Popen(comd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate()
    msgs.info("Header generated: " + os.path.join(workdir, catname[:-5]+".head"))

    if log:
        logfile = open(os.path.join(workdir, imgname[:-5]+".scamp.log"), "w")
        logfile.write("SCAMP was called with :\n")
        logfile.write(" ".join(comd))
        logfile.write("\n\n####### stdout #######\n")
        logfile.write(out.decode("utf-8"))
        logfile.write("\n####### stderr #######\n")
        logfile.write(err.decode("utf-8"))
        logfile.write("\n")
        logfile.close()
        msgs.info("Processing log generated: " + os.path.join(workdir, imgname[:-5]+".scamp.log"))
    if delete:
        os.system("rm " + os.path.join(workdir,"*.scamp"))

def scampall(imglist, config=None, workdir='./', QAdir='./', defaultconfig='pyphot', delete=False, log=True):

    for imgname in imglist:
        if config is not None:
            this_config = config.copy() # need to copy this since the config would be possibly changed in scampone!
        else:
            this_config = None
        scampone(imgname, config=this_config, workdir=workdir, QAdir=QAdir, defaultconfig=defaultconfig, delete=delete, log=log)
