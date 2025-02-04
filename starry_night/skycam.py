from starry_night import sql
import pandas as pd
import numpy as np
import matplotlib as mpl
import matplotlib.pyplot as plt
from matplotlib import rc, cm
from mpl_toolkits.axes_grid.inset_locator import inset_axes

import ephem
import sys
from time import sleep

from astropy.io import fits
from astropy.time import Time
from scipy.io import matlab
from scipy.ndimage.measurements import label
from io import BytesIO
from skimage.io import imread
from skimage.color import rgb2gray
import skimage.filters
import warnings

from datetime import datetime, timedelta

from pkg_resources import resource_filename
from os.path import join
import requests
import logging

from re import split
from hashlib import sha1
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError, InternalError
from IPython import embed



def lin(x,m,b):
    return m*x+b

def transmission(x, a, c):
    '''
    return atmospheric transmission of planar model
    '''
    x = np.pi/2 -x
    return a*np.exp(-c * (1/np.cos(x) - 1))
def transmission2(x, a, c):
    '''
    return atmospheric transmission of planar model with correction (Young - 1974)
    '''
    x = np.pi/2 -x
    return a * np.exp(-c * (1/np.cos(x)*(1-0.0012*(1/np.cos(x)**2 - 1)) - 1))


def transmission3(x, a, c):
    '''
    return atmospheric transmission of spheric model with elevated observer
    This model does not return 1.0 for zenith angle so we subtract airM_0 instead in the end
    '''
    yObs=2.2
    yAtm=9.5
    rEarth=6371.0

    x = (np.pi/2 -x)
    r = rEarth / yAtm
    y = yObs / yAtm

    airMass = np.sqrt( ( r + y )**2 * np.cos(x)**2 + 2.*r*(1.-y) - y**2 + 1.0 ) - (r+y)*np.cos(x)
    airM_0 = np.sqrt( ( r + y )**2 + 2.*r*(1.-y) - y**2 + 1.0 ) - (r+y)
    return a* np.exp(-c * (airMass - airM_0 ))

'''
y1 = sk.transmission(x, 1, 0.57)
y2 = sk.transmission2(x, 1, 0.57)
y4 = sk.transmission3(x, 1, 0.67)

plt.plot(x,y1, label='Planar')
plt.plot(x,y2, label='Planar korrektur')
plt.plot(x,y4, label='geom 2200m')
plt.grid()
plt.ylim((0, 1.1))
plt.legend(loc='lower right')
plt.show()
'''

class TooEarlyError(Exception):
    pass

def get_last_modified(url, timeout):
    ret = requests.head(url, timeout=timeout)
    date = datetime.strptime(
        ret.headers['Last-Modified'],
        '%a, %d %b %Y %H:%M:%S GMT'
    )
    return date


def downloadImg(url, timeout=None):
    '''
    Download image from URL and return a dict with 'img' and 'timestamp'

    Download will only happen, if the website was updated since the last download AND the SHA1
    hashsum differs from the previous image because sometime a website might refresh without
    updating the image.
    Works with fits, mat and all common image filetypes.
    '''
    log = logging.getLogger(__name__)
    if not hasattr(downloadImg, 'lastMod'):
        downloadImg.lastMod = datetime(1,1,1)
        downloadImg.hash = ''
    logging.getLogger('requests').setLevel(logging.WARNING)

    # only download if time since last image is > than wait
    mod = get_last_modified(url, timeout=timeout)
    if downloadImg.lastMod == mod:
        raise TooEarlyError()
    else:
        downloadImg.lastMod = mod

    # download image data and double check if this really is a new image
    log.info('Downloading image from {}'.format(url))
    ret = requests.get(url, timeout=timeout)
    if downloadImg.hash == sha1(ret.content).hexdigest():
        raise TooEarlyError()
    else:
        downloadImg.hash = sha1(ret.content).hexdigest()
    if url.split('.')[-1] == 'mat':
        data = matlab.loadmat(BytesIO(ret.content))
        for d in list(data.values()):
            # loop through all keys and treat the first array with size > 100x100 as image
            # that way the name of the key does not matter
            try:
                if d.shape[0] > 100 and d.shape[1] > 100:
                    img = d
            except AttributeError:
                pass
            try:
                timestamp = datetime.strptime(d[0], '%Y/%m/%d %H:%M:%S')
            except (IndexError, TypeError, ValueError):
                pass
    elif url.split('.')[-1] == 'FIT':
        hdulist = fits.open(BytesIO(ret.content), ignore_missing_end=True)
        img = hdulist[0].data+2**16/2
        timestamp = datetime.strptime(
                        hdulist[0].header['UTC'],
                        '%Y/%m/%d %H:%M:%S')

    else:
        img = rgb2gray(imread(url, ))
        timestamp = get_last_modified(url, timeout=timeout)
        
    return {
        'img' : img,
        'timestamp' : timestamp,
        }



def getBlobsize(img, thresh, limit=0):
    '''
    Returns size of the blob in the center of img.
    If the blob is bigger than limit, limit gets returned immideatly.

    A blob consists of all 8 neighboors that are bigger than 'thresh' and their neighboors respectively.
    '''
    if thresh <= 0:
        raise ValueError('Thresh > 0 required')
    if img.shape[0]%2 == 0 or img.shape[1]%2==0:
        raise IndexError('Only odd sized arrays are supported. Array shape:{}'.format(img.shape))
    if limit == 0:
        limit = img.shape[0]*img.shape[1]

    center = (img.shape[0]//2, img.shape[1]//2)

    # if all pixels are above threshold then return max blob size
    if thresh <= np.min(img):
        return np.minimum(limit, img.shape[0]*img.shape[1])
    
    # work on local copy
    tempImg = img.copy()
    tempImg[~np.isfinite(tempImg)] = 0

    nList = list()
    count = 0

    # fill list with pixels and count them
    nList.append(center)
    while len(nList) > 0:
        x,y = nList.pop(0)

        for i in (-1,0,1):
            for j in (-1,0,1):
                if x+i<0 or x+i>=img.shape[0] or y+j<0 or y+j>=img.shape[1]:
                    pass
                elif tempImg[x+i,y+j] >= thresh:
                    count += 1
                    tempImg[x+i,y+j] = 0
                    nList.append((x+i,y+j))
        if count >= limit:
            return limit
    return count
    

def run():
    log = logging.getLogger(__name__)
    wait = 120  #wait 120 seconds between downloads
    old_date = datetime(2000, 1, 1)
    while True:
        # allsky images are only taken during night time
        if True: #datetime.utcnow().hour > 17 or datetime.utcnow().hour < 9:
            log.info('Downloading image')
            try:
                date = get_last_modified(url, timeout=5)
            except (KeyboardInterrupt, SystemExit):
                exit(0)
            except Exception as e:
                log.error(
                    'Fetching Last-Modified failed with error:\n\t{}'.format(e)
                )
                sleep(10)
                continue

            if date > old_date:
                log.info('Found new file, downloading')
                try:
                    log.debug('debug test')
                    old_date = date
                    #downloading an image may take some time. So try next download right after the first one
                    continue 
                except (KeyboardInterrupt, SystemExit):
                    exit(0)
                except Exception as e:
                    log.error('Download failed with error: \n\t{}'.format(e))
                    sleep(10)
                    continue
            else:
                log.info('No new image found')
        else:
                log.info('Daytime - no download')

        sleep(wait)


def theta2r(theta, radius, how='lin'):
    '''
    convert angle to the optical axis into pixel distance to the camera
    center

    assumes linear angle projection function or equisolid angle projection function (Sigma 4.5mm f3.5)
    '''
    if how == 'lin':
        return radius / (np.pi/2) * theta
    else:
        return 2/np.sqrt(2) * radius * np.sin(theta/2)

def r2theta(r, radius, how='lin', mask=False):
    '''
    convert angle to the optical axis into pixel distance to the camera
    center

    assumes linear angle projection function or equisolid angle projection function (Sigma 4.5mm f3.5)

    Returns: -converted coords,
             -mask with valid values
    '''
    if how == 'lin':
        return r / radius * (np.pi/2)
    else:
        if mask:
            return np.arcsin(r / (2/np.sqrt(2)) / radius) * 2, r/(2/np.sqrt(2))/radius < 1
        else:
            return np.arcsin(r / (2/np.sqrt(2)) / radius) * 2


def horizontal2image(az, alt, cam):
    '''
    convert azimuth and altitude to pixel_x, pixel_y

    Parameters
    ----------
    az : float or array-like
        the azimuth angle in radians
    alt : float or array-like
        the altitude angle in radians
    cam: dictionary
        contains zenith position, radius

    Returns
    -------
    pixel_x : number or array-like
        x cordinate in pixels for the given az, alt
    pixel_y : number or array-like
        y cordinate in pixels for the given az, alt
    '''

    try:
        x = np.float(cam['zenith_x']) + theta2r(np.pi/2 - alt,
                np.float(cam['radius']),
                how=cam['angleprojection']
                ) * np.cos(az+np.deg2rad(np.float(cam['azimuthoffset'])))
        y = np.float(cam['zenith_y']) - theta2r(np.pi/2 - alt,
                np.float(cam['radius']),
                how=cam['angleprojection']
                ) * np.sin(az+np.deg2rad(np.float(cam['azimuthoffset'])))
    except:
        raise
    return x, y

def find_matching_pos(img_timestamp, time_pos_list):
    '''
    Returns 'Ra' and 'Dec' entry from 'time_pos_list' 
    that has the closest timestamp to 'img_timestamp'+5min

    Since the lidar operates all the time but we only take images every few minutes 
    we need to find out where the lidar was looking at that point in time when we took the image
    '''
    # select measurements that were taken not later than 5 minutes before the image
    subset = time_pos_list.query('1/24/60 * 5 < MJD - {} < 1/24/60*10'.format(img_timestamp)).sort_values('MJD')
    closest = subset[subset.MJD==subset.MJD.min()]
    return closest[['ra','dec']]



def obs_setup(properties):
    ''' creates an ephem.Observer for the MAGIC Site at given date '''
    obs = ephem.Observer()
    obs.lon = '-17:53:28'
    obs.lat = '28:45:42'
    obs.elevation = 2200
    obs.epoch = ephem.J2000
    return obs


def equatorial2horizontal(ra, dec, observer):
    '''
    Transforms from right ascension, declination to azimuth, altitude for
    the given observer.
    Formulas are taken from https://goo.gl/1wMU4u

    Parameters
    ----------

    ra : number or array-like
        right ascension in radians of the object of interest

    dec : number or array-like
        declination in radians of the object of interest

    observer : ephem.Observer
        the oberserver for which azimuth and altitude are calculated

    Returns
    -------
    az : number or numpy.ndarray
        azimuth in radians for the given ra, dec
    alt : number or numpy.ndarray
        altitude in radians for the given ra, dec
    '''

    obs_lat = float(observer.lat)

    h = observer.sidereal_time() - ra
    alt = np.arcsin(np.sin(obs_lat) * np.sin(dec) + np.cos(obs_lat) * np.cos(dec) * np.cos(h))
    az = np.arctan2(np.sin(h), np.cos(h) * np.sin(obs_lat) - np.tan(dec)*np.cos(obs_lat))

    # correction for camera orientation
    az = np.mod(az+np.pi, 2*np.pi)
    return az, alt


def celObjects_dict(config):
    '''
    Read the given star catalog, add planets from ephem and fill sun and moon with NaNs
    For horizontal coordinates 'update_star_position()' needs to be called next.

    Returns: dictionary with celestial objects
    '''
    log = logging.getLogger(__name__)
    
    log.debug('Loading stars')
    catalogue = resource_filename('starry_night', 'data/catalogue_10vmag_1degFilter.csv')
    try:
        stars = pd.read_csv(
            catalogue,
            sep=',',
            comment='#',
            header=0,
            skipinitialspace=False,
            index_col=0,
        )
    except OSError as e:
        log.error('Star catalogue not found: {}'.format(e))
        sys.exit(1)
    #stars = stars.to_numeric()

    # transform degrees to radians
    stars.ra = np.deg2rad(stars.ra)
    stars.dec = np.deg2rad(stars.dec)

    stars['altitude'] = np.NaN
    stars['azimuth'] = np.NaN

    # add the planets
    planets = pd.DataFrame()
    for planet in ['Mercury', 'Venus', 'Mars', 'Jupiter', 'Saturn', 'Uranus', 'Neptune']:
        data = {
            'ra': np.NaN,
            'dec': np.NaN,
            'altitude' : np.NaN,
            'azimuth' : np.NaN,
            'gLon': np.NaN,
            'gLat': np.NaN,
            'vmag': np.NaN,
            'name': planet,
        }
        planets = planets.append(data, ignore_index=True)

    # add points_of_interest
    log.debug('Add points of interest')
    try:
        points_of_interest = pd.read_csv(
            config['analysis']['points_of_interest'],
            sep=',',
            comment='#',
            header=0,
            skipinitialspace=False,
            index_col=None,
        )
    except OSError as e:
        log.debug('File with points of interest not found: {} We will now check internal package files...'.format(e))
        try:
            poi_filename = resource_filename('starry_night', join('data',config['analysis']['points_of_interest']))
            points_of_interest = pd.read_csv(
                poi_filename,
                sep=',',
                comment='#',
                header=0,
                skipinitialspace=False,
                index_col=None,
            )
        except OSError as e:
            log.error('File with points of interest not found: {}'.format(e))
            sys.exit(1)
        else:
            log.debug('Found {}'.format(poi_filename))

    points_of_interest['altitude'] = np.NaN
    points_of_interest['azimuth'] = np.NaN
    points_of_interest['radius'] = float(config['analysis']['poi_radius'])
    points_of_interest['ra'] *= np.pi/180 
    points_of_interest['dec'] *= np.pi/180

    # add moon
    moonData = {
        'moonPhase' : np.NaN,
        'altitude' : np.NaN,
        'azimuth' : np.NaN,
    }
    # add sun
    sunData = {
        'altitude' : np.NaN,
        'azimuth' : np.NaN,
    }

    return dict({'stars': stars,
        'planets': planets,
        'points_of_interest' : points_of_interest,
        'sun': sunData,
        'moon': moonData,
        })


def update_star_position(data, observer, conf, crop, args):
    '''
    Takes the dictionary from 'star_planets_sun_moon_dict(observer)'
    and calculates the current position of each object in the sky
    also sets position of sun and moon (were filled with NaNs so far)
    Objects that are not within the camera limits (vmag, altitude, crop...) get removed.

    Returns: dictionary with updated positions
    '''
    log = logging.getLogger(__name__)

    # include moon data
    log.debug('Loading moon')
    moon = ephem.Moon()
    moon.compute(observer)
    moonData = {
        'moonPhase' : float(moon.moon_phase),
        'altitude' : float(moon.alt),
        'azimuth' : float(moon.az),
    }

    # include sun data
    log.debug('Load Sun')
    sun = ephem.Sun()
    sun.compute(observer)
    sunData = {
        'altitude' : float(sun.alt),
        'azimuth' : float(sun.az),
    }

    # add the planets
    log.debug('Loading planets')
    sol_objects = [
        ephem.Mercury(),
        ephem.Venus(),
        ephem.Mars(),
        ephem.Jupiter(),
        ephem.Saturn(),
        ephem.Uranus(),
        ephem.Neptune(),
    ]
    planets = pd.DataFrame()
    for sol_object in sol_objects:
        sol_object.compute(observer)
        equatorial = ephem.Equatorial(sol_object.g_ra, sol_object.g_dec, epoch=ephem.J2000)
        galactic = ephem.Galactic(equatorial)
        p = {
            'ra': float(sol_object.a_ra),
            'dec': float(sol_object.a_dec),
            'gLon': float(galactic.lon),
            'gLat': float(galactic.lat),
            'vmag': float(sol_object.mag),
            'azimuth': float(sol_object.az),
            'altitude': float(sol_object.alt),
            'name': sol_object.name,
        }
        planets = planets.append(p, ignore_index=True)
    planets.set_index('name', inplace=True)

    # make a copy here, because we will need ALL stars later again
    # append lidar position from positioning file if any
    # append Total_sky object 
    # update all objects
    # remove objects that are not within the limits
    stars = data['stars'].copy()
    points_of_interest = data['points_of_interest'].copy()
    if args['-p']:
        lidar = find_matching_pos(Time(data['timestamp']).mjd, data['positioning_file'])/180*np.pi
        lidar['name'] = 'Lidar'
        lidar['ID'] = -2
        lidar['radius'] = float(conf['analysis']['poi_radius'])
        points_of_interest = points_of_interest.append(lidar, ignore_index=True)

    stars['azimuth'], stars['altitude'] = equatorial2horizontal(
        stars.ra, stars.dec, observer,
    )
    points_of_interest['azimuth'], points_of_interest['altitude'] = equatorial2horizontal(
        points_of_interest.ra, points_of_interest.dec, observer,
    )
    points_of_interest.append({
        'name': 'Total_sky',
        'azimuth': 0,
        'altitude': np.pi/2,
        'ID': -1,
        'radius': float(conf['image']['openingangle']),
    }, ignore_index=True)

    try:
        stars.query('altitude > {} & vmag < {}'.format(np.deg2rad(90 - float(conf['image']['openingangle'])), conf['analysis']['vmaglimit']), inplace=True)
        planets.query('altitude > {} & vmag < {}'.format(np.deg2rad(90 - float(conf['image']['openingangle'])), conf['analysis']['vmaglimit']), inplace=True)
        points_of_interest.query('altitude > {}'.format(np.deg2rad(90 - float(conf['image']['openingangle']))), inplace=True)
    except:
        log.error('Using altitude or vmag limit failed!')
        raise

    # calculate angle to moon
    log.debug('Calculate Angle to Moon')
    stars['angleToMoon'] = np.arccos(np.sin(stars.altitude.values)*
        np.sin(moon.alt) + np.cos(stars.altitude.values)*np.cos(moon.alt)*
        np.cos((stars.azimuth.values - moon.az)))
    planets['angleToMoon'] = np.arccos(np.sin(planets.altitude.values)*
        np.sin(moon.alt) + np.cos(planets.altitude.values)*np.cos(moon.alt)*
        np.cos((planets.azimuth.values - moon.az)))
    points_of_interest['angleToMoon'] = np.arccos(np.sin(points_of_interest.altitude.values)*
        np.sin(moon.alt) + np.cos(points_of_interest.altitude.values)*np.cos(moon.alt)*
        np.cos((points_of_interest.azimuth.values - moon.az)))

    # remove stars and planets that are too close to moon
    stars.query('angleToMoon > {}'.format(np.deg2rad(float(conf['analysis']['minAngleToMoon']))), inplace=True)
    planets.query('angleToMoon > {}'.format(np.deg2rad(float(conf['analysis']['minAngleToMoon']))), inplace=True)


    # calculate x and y position
    log.debug('Calculate x and y')
    stars['x'], stars['y'] = horizontal2image(stars.azimuth, stars.altitude, cam=conf['image'])
    planets['x'], planets['y'] = horizontal2image(planets.azimuth, planets.altitude, cam=conf['image'])
    points_of_interest['x'], points_of_interest['y'] = horizontal2image(points_of_interest.azimuth, points_of_interest.altitude, cam=conf['image'])
    moonData['x'], moonData['y'] = horizontal2image(moonData['azimuth'], moonData['altitude'], cam=conf['image'])
    sunData['x'], sunData['y'] = horizontal2image(sunData['azimuth'], sunData['altitude'], cam=conf['image'])

    # remove stars and planets that are withing cropping area
    res = list(map(int, split('\\s*,\\s*', conf['image']['resolution'])))
    stars.query('0 < x < {} & 0 < y < {}'.format(res[0] ,res[1]), inplace=True)
    planets.query('0 < x < {} & 0 < y < {}'.format(res[0] ,res[1]), inplace=True)
    points_of_interest.query('0 < x < {} & 0 < y < {}'.format(res[0] ,res[1]), inplace=True)
    stars = stars[stars.apply(lambda s, crop=crop: ~crop[int(s['y']), int(s['x'])], axis=1)]
    planets = planets[planets.apply(lambda p, crop=crop: ~crop[int(p['y']), int(p['x'])], axis=1)]
    points_of_interest = points_of_interest[points_of_interest.apply(lambda s, crop=crop: ~crop[int(s['y']), int(s['x'])], axis=1)]

    return {'stars':stars, 'planets':planets, 'points_of_interest': points_of_interest, 'moon': moonData, 'sun': sunData}

def findLocalStd(img, x, y, radius):
    '''
    ' Returns value of brightest pixel within radius
    '''
    try:
        x = int(x)
        y = int(y)
    except TypeError:
        x = x.astype(int)
        y = y.astype(int)
    
    # get interval border
    x_interval = np.max([x-radius,0]) , np.min([x+radius+1, img.shape[1]])
    y_interval = np.max([y-radius,0]) , np.min([y+radius+1, img.shape[0]])
    radius = x_interval[1]-x_interval[0] , y_interval[1]-y_interval[0]

    # do subselection
    subImg = img[y_interval[0]:y_interval[1] , x_interval[0]:x_interval[1]]
    try:
        return np.nanstd(subImg.flatten())
    except RuntimeWarning:
        print('NAN')
        return 0
    except ValueError:
        print('Star outside image')
        return 0


def findLocalMean(img, x, y, radius):
    '''
    ' Returns value of brightest pixel within radius
    '''
    try:
        x = int(x)
        y = int(y)
    except TypeError:
        x = x.astype(int)
        y = y.astype(int)
    
    # get interval border
    x_interval = np.max([x-radius,0]) , np.min([x+radius+1, img.shape[1]])
    y_interval = np.max([y-radius,0]) , np.min([y+radius+1, img.shape[0]])
    radius = x_interval[1]-x_interval[0] , y_interval[1]-y_interval[0]

    # do subselection
    subImg = img[y_interval[0]:y_interval[1] , x_interval[0]:x_interval[1]]
    try:
        return np.nanmean(subImg.flatten())
    except RuntimeWarning:
        print('NAN')
        return 0
    except ValueError:
        print('Star outside image')
        return 0


def findLocalMaxValue(img, x, y, radius):
    '''
    ' Returns value of brightest pixel within radius
    '''
    try:
        x = int(x)
        y = int(y)
    except TypeError:
        x = x.astype(int)
        y = y.astype(int)
    
    # get interval border
    x_interval = np.max([x-radius,0]) , np.min([x+radius+1, img.shape[1]])
    y_interval = np.max([y-radius,0]) , np.min([y+radius+1, img.shape[0]])
    radius = x_interval[1]-x_interval[0] , y_interval[1]-y_interval[0]

    # do subselection
    subImg = img[y_interval[0]:y_interval[1] , x_interval[0]:x_interval[1]]
    try:
        return np.nanmax(subImg.flatten())
    except RuntimeWarning:
        print('NAN')
        return 0
    except ValueError:
        print('Star outside image')
        return 0


def findLocalMaxPos(img, x, y, radius):
    '''
    ' Returns x and y position of brightest pixel within radius
    ' If all pixel have equal brightness, current position is returned
    '''
    try:
        x = int(x)
        y = int(y)
    except TypeError:
        x = x.astype(int)
        y = y.astype(int)
    # get interval border
    x_interval = np.max([x-radius,0]) , np.min([x+radius+1, img.shape[1]])
    y_interval = np.max([y-radius,0]) , np.min([y+radius+1, img.shape[0]])
    radius = x_interval[1]-x_interval[0] , y_interval[1]-y_interval[0]
    subImg = img[y_interval[0]:y_interval[1] , x_interval[0]:x_interval[1]]
    if np.max(subImg) != np.min(subImg):
        try:
            maxPos = np.nanargmax(subImg)
            x = (maxPos%radius[0])+x_interval[0]
            y = (maxPos//radius[0])+y_interval[0]
        except ValueError:
            return pd.Series({'maxX':0, 'maxY':0})
    return pd.Series({'maxX':int(x), 'maxY':int(y)})


def getImageDict(filepath, config, crop=None, fmt=None):
    '''
    Open an image file and return its content as a numpy array.
    
    input:
        filename: full or relativ path to image
        crop: crop image to a circle with center and radius
        fmt: format timestring like 'gtc_allskyimage_%Y%m%d_%H%M%S.jpg'
            used for parsing the date from filename
    Returns: Dictionary with image array and timestamp datetime object
    '''
    log = logging.getLogger(__name__)

    # get image type from filename
    filename = filepath.split('/')[-1].split('.')[0]
    filetype= filepath.split('.')[-1]

    # read mat file
    if filetype == 'mat':
        data = matlab.loadmat(filepath)
        img = data['pic1']
        time = datetime.strptime(
            data['UTC1'][0], '%Y/%m/%d %H:%M:%S'
        )

    # read fits file
    elif (filetype == 'fits') or (filetype == 'gz'):
        hdulist = fits.open(filepath, ignore_missing_end=True)
        img = hdulist[0].data
        time = datetime.strptime(
            hdulist[0].header['TIMEUTC'],
            '%Y-%m-%d %H:%M:%S'
        )
    else:
        # read normal image file
        try:
            img = imread(filepath, mode='L', as_grey=True)
        except (FileNotFoundError, OSError, ValueError) as e:
            log.error('Error reading file \'{}\': {}'.format(filename+'.'+filetype, e))
            return
        try:
            if fmt is None:
                time = datetime.strptime(filename, config['properties']['timeformat'])
            else:
                time = datetime.strptime(filename, fmt)
        
        except ValueError:
            fmt = (config['properties']['timeformat'] if fmt is None else fmt)
            log.error('{},{}'.format(filename,filepath))
            log.error('Unable to parse image time from filename. Maybe format is wrong: {}'.format(fmt))
            raise
            sys.exit(1)
    time += timedelta(minutes=float(config['properties']['timeoffset']))
    return dict({'img': img, 'timestamp': time})

def update_crop_moon(crop_mask, moon, conf):
    nrows, ncols = crop_mask.shape
    row, col = np.ogrid[:nrows, :ncols]
    x = moon['x']
    y = moon['y']
    r = theta2r(float(conf['analysis']['minAngleToMoon'])/180*np.pi, float(conf['image']['radius']), how=conf['image']['angleprojection'])
    crop_mask = crop_mask | ((row - y)**2 + (col - x)**2 < r**2)
    return crop_mask


    
def get_crop_mask(img, crop):
    '''
    crop is dictionary with cropping information
    returns a boolean array in size of img: False got cropped; True not cropped 
    '''
    nrows, ncols = img.shape
    row, col = np.ogrid[:nrows, :ncols]
    disk_mask = np.full((nrows, ncols), False, dtype=bool)

    try:
        x = list(map(int, split('\\s*,\\s*', crop['crop_x'])))
        y = list(map(int, split('\\s*,\\s*', crop['crop_y'])))
        r = list(map(int, split('\\s*,\\s*', crop['crop_radius'])))
        inside = list(map(int, split('\\s*,\\s*', crop['crop_deleteinside'])))
        for x,y,r,inside in zip(x,y,r,inside):
            if inside == 0:
                disk_mask = disk_mask | ((row - y)**2 + (col - x)**2 > r**2)
            else:
                disk_mask = disk_mask | ((row - y)**2 + (col - x)**2 < r**2)
    except ValueError:
        log = logging.getLogger(__name__)
        log.error('Cropping failed, maybe there is a typing error in the config file?')
        disk_mask = np.full((nrows, ncols), False, dtype=bool)

    return disk_mask


def loadImageTime(filename):
    # assuming that the filename only contains numbers of timestamp
    timestamp = re.findall('\d{2,}', filename)
    timestamp = list(map(int, timestamp))

    return datetime(*timestamp)


# display fits image on screen
def dispFits(image):
    fig = plt.figure()
    ax = fig.add_axes([0.05, 0.05, 0.95, 0.95])
    vmin = np.nanpercentile(image, 0.5)
    vmax = np.nanpercentile(image, 99.5)
    image = (image - vmin)*(1000./(vmax-vmin))
    vmin = np.nanpercentile(image, 0.5)
    vmax = np.nanpercentile(image, 99.5)
    ax.imshow(image, vmin=vmin, vmax=vmax, cmap='gray', interpolation='none')
    plt.show()


def dispHist(image):
    fig = plt.figure()
    ax = fig.add_axes([0.05, 0.05, 0.95, 0.95])
    '''
    image = (image - vmin)*(1000./(vmax-vmin))
    vmin = np.nanpercentile(image, 0.5)
    vmax = np.nanpercentile(image, 99.5)
    '''
    plt.hist(image[~np.isnan(image)].ravel(), bins=100, range=(-150,2000))
    plt.show()


def isInRange(position, stars, rng, unit='deg'):
    '''
    Returns true or false for each star in stars if distance between star and position<rng

    If unit= "pixel" position and star must have attribute .x and .y in pixel and rng is pixel distance
    If unit= "deg" position and star must have attribute .ra and .dec in degree 0<360 and rng is degree
    '''
    if rng < 0:
        raise ValueError
    
    if unit == 'pixel':
        try:
            return ((position.x - stars.x)**2 + (position.y - stars.y)**2 <= rng**2)
        except AttributeError as e:
            log.error('Pixel value needed but object has no x/y attribute. {}'.format(e))
            sys.exit(1)
    elif unit == 'deg':
        try:
            ra1 = position['ra']
            dec1 = position['dec']
            deltaDeg = 2*np.arcsin(np.sqrt(np.sin((dec1-stars.dec)/2)**2 + np.cos(dec1)*np.cos(stars.dec)*np.sin((ra1-stars.ra)/2)**2))
        except (AttributeError, KeyError) as e:
            try:
                alt1 = position['altitude']
                az1 = position['azimuth']
                deltaDeg = 2*np.arcsin(np.sqrt(np.sin((az1-stars.azimuth)/2)**2 + np.cos(az1)*np.cos(stars.azimuth)*np.sin((alt1-stars.altitude)/2)**2))
            except (AttributeError,KeyError) as e:
                log = logging.getLogger(__name__)
                log.error('Degree value needed but object has no ra/dec an no alt/az attribute. {}'.format(e))

                sys.exit(1)

        return deltaDeg <= np.deg2rad(rng)
    else:
        raise ValueError('unit has unknown type')


def calc_star_percentage(position, stars, rng, lim=1, unit='deg', weight=False):
    '''
    Returns: percentage of stars within range of position that are visible 
             and -1 if no stars in range
    
    Position is dictionary and can contain Ra,Dec and/or x,y
    Range is degree or pixel radius depending on whether unit is 'grad' or 'pixel'
    Lim > 0: is limit visibility that separates visible stars from not visible. [0.0 - 1.0].
    lim < 0 then all stars in range will be used and 'visible' is a weight factor
    Weight = True: each star is multiplied by weight [100**(1/5)]**-magnitude -> bright stars have more impact
    '''

    if rng < 0:
        starsInRange = stars
    else:
        starsInRange = stars[isInRange(position, stars, rng, unit)]

    if starsInRange.empty:
        return -1

    if lim >= 0:
        if weight:
            vis = np.sum(np.power(100**(1/5), -starsInRange.query('visible >= {}'.format(lim)).vmag.values))
            notVis = np.sum(np.power(100**(1/5), -starsInRange.query('visible < {}'.format(lim)).vmag.values))
            percentage = vis/(vis+notVis)
        else:
            percentage = len(starsInRange.query('visible >= {}'.format(lim)).index)/len(starsInRange.index)
    else:
        if weight:
            percentage = np.sum(starsInRange.visible.values * np.power(100**(1/5),-starsInRange.vmag.values)) / \
                np.sum(np.power(100**(1/5),-starsInRange.vmag.values))
        else:
            percentage = np.mean(starsInRange.visible.values)


    return percentage


def calc_cloud_map(stars, rng, img_shape, weight=False):
    '''
    Input:  stars - pandas dataframe
            rng - sigma of gaussian kernel (integer)
            img_shape - size of cloudiness map in pixel (tuple)
            weight - use magnitude as weight or not (boolean)
    Returns: Cloudines map of the sky. 1=cloud, 0=clear sky

    Cloudiness is percentage of visible stars in local area. Stars get weighted by
    distance (gaussian) and star magnitude 2.5^magnitude.
    Instead of a computationally expensive for-loop we use two 2D histograms of the stars (weighted)
    and convolve them with an gaussian kernel resulting in some kind of 'density map'.
    Division of both maps yields the desired cloudines map.
    '''
    if weight:
        scattered_stars_visible,_,_ = np.histogram2d(x=stars.y.values, y=stars.x.values, weights=stars.visible.values * 2.5**-stars.vmag.values, bins=img_shape, range=[[0,img_shape[0]],[0,img_shape[1]]])
        scattered_stars,_,_ = np.histogram2d(stars.y.values, stars.x.values, weights=np.ones(len(stars.index)) * 2.5**-stars.vmag.values, bins=img_shape, range=[[0,img_shape[0]],[0,img_shape[1]]])
        density_visible = skimage.filters.gaussian(scattered_stars_visible, rng)
        density_all = skimage.filters.gaussian(scattered_stars, rng)
    else:
        scattered_stars_visible,_,_ = np.histogram2d(x=stars.y.values, y=stars.x.values, weights=stars.visible.values, bins=img_shape, range=[[0,img_shape[0]],[0,img_shape[1]]])
        scattered_stars,_,_ = np.histogram2d(stars.y.values, stars.x.values, weights=np.ones(len(stars.index)), bins=img_shape, range=[[0,img_shape[0]],[0,img_shape[1]]])
        density_visible = skimage.filters.gaussian(scattered_stars_visible, rng, mode='mirror')
        density_all = skimage.filters.gaussian(scattered_stars, rng, mode='mirror')
    with np.errstate(divide='ignore',invalid='ignore'):
        cloud_map = np.true_divide(density_visible, density_all)
        cloud_map[~np.isfinite(cloud_map)] = 0
    return 1-cloud_map



def filter_catalogue(catalogue, rng):
    '''
    Loop through all possible pairs of stars and remove less bright star if distance is < rng

    Input:  catalogue - Pandas dataframe (ra and dec in degree)
            rng - Min distance between stars in degree

    Returns: List of indexes that remain in catalogue
    '''
    log = logging.getLogger(__name__)
    try:
        c = catalogue.sort_values('vmag', ascending=True)
        reference = np.deg2rad(c[['ra','dec']].values)
        index = c.index
    except KeyError:
        log.error('Key not found. Please check that your catalogue is labeled correctly')
        raise
    
    i1 = 0 #star index that is used as filter base
    while i1 < len(reference)-1:
        print('Items left: {}/{}'.format(i1,len(reference)-1))
        deltaDeg = np.rad2deg(2*np.arcsin(np.sqrt(np.sin((reference[i1,1]-reference[:,1])/2)**2 + np.cos(reference[i1,1])*np.cos(reference[:,1])*np.sin((reference[i1,0]-reference[:,0])/2)**2)))
        keep = deltaDeg > rng
        keep[:i1+1] = True #don't remove stars that already passed the filter
        reference = reference[keep]
        index = index[keep]
        i1+=1
    return index


def process_image(images, data, config, args):
    '''
    This function applies all neccessary calculations to an image and returns the results.
    Use it in the main loop!
    '''
    log = logging.getLogger(__name__)

    output = dict()
    if not images:
        return


    log.info('Processing image taken at: {}'.format(images['timestamp']))
    observer = obs_setup(config['properties'])
    observer.date = images['timestamp']
    data['timestamp'] = images['timestamp']

    # stop processing if sun is too high or config file does not match
    if images['img'].shape[1]  != int(config['image']['resolution'].split(',')[0]) or images['img'].shape[0]  != int(config['image']['resolution'].split(',')[1]):
        log.error('Resolution does not match: {}!={}. Wrong config file?'.format(c_res, i_res))
        return
    sun = ephem.Sun()
    sun.compute(observer)
    moon = ephem.Moon()
    moon.compute(observer)
    '''
    if not args['--daemon']:
        if np.rad2deg(sun.alt) > -10:
            log.info('Sun too high: {}° above horizon. We start below -10°, current time: {}'.format(np.round(np.rad2deg(sun.alt),2), images['timestamp']))
            return 
        elif np.rad2deg(moon.alt) > -10:
            log.info('Moon too high: {}° above horizon. We start below -10°, current time: {}'.format(np.round(np.rad2deg(moon.alt),2), images['timestamp']))
            return
    '''

    # put timestamp and hash sum into output dict
    output['timestamp'] = images['timestamp']
    try:
        output['hash'] = sha1(images['img'].data).hexdigest()
    except BufferError:
        output['hash'] = sha1(np.ascontiguousarray(images['img']).data).hexdigest()
        

    # create cropping array to mask unneccessary image regions.
    crop_mask = get_crop_mask(images['img'], config['crop'])

    # update celestial objects (ignore planets, because they are bigger than stars and mess up the detection)
    celObjects = update_star_position(data, observer, config, crop_mask, args)
    stars = pd.concat([celObjects['stars'],])# celObjects['planets']])
    if stars.empty:
        log.error('No stars in DataFrame. Maybe all got removed by cropping? No analysis possible.')
        return
    crop_mask = update_crop_moon(crop_mask, celObjects['moon'], config)
    images['img'][crop_mask] = np.NaN
    output['brightness_mean'] = np.nanmean(images['img'])
    output['brightness_std'] = np.nanmean(images['img'])
    img = images['img']
    
    # calculate response of stars
    if args['--kernel']:
        kernelSize = float(args['--kernel']),
        #np.arange(1, int(args['--kernel'])+1, 5)
        stars_orig = stars.copy()
    else:
        kernelSize = [float(config['analysis']['kernelsize'])]
    kernelResults = list()

    for k in kernelSize:
        log.debug('Apply image filters. Kernelsize = {}'.format(k))

        # undo all changes, if we are in a loop
        if len(kernelSize) > 1:
            stars = stars_orig.copy()
        stars['kernel'] = k

        gauss = skimage.filters.gaussian(img, sigma=k)

        # chose the response function
        if args['--function'] == 'All' or args['--ratescan']:
            grad = (img - np.roll(img, 1, axis=0)).clip(min=0)**2 + (img - np.roll(img, 1, axis=1)).clip(min=0)**2
            sobel = skimage.filters.sobel(img).clip(min=0)
            lap = skimage.filters.laplace(gauss, ksize=3).clip(min=0)

            grad[crop_mask] = np.NaN
            sobel[crop_mask] = np.NaN
            lap[crop_mask] = np.NaN
            images['grad'] = grad
            images['sobel'] = sobel
            images['lap'] = lap
            resp = lap
        elif args['--function'] == 'DoG':
            resp = skimage.filters.gaussian(img, sigma=k) - skimage.filters.gaussian(img, sigma=1.6*k)
        elif args['--function'] == 'LoG':
            resp = skimage.filters.laplace(gauss, ksize=3).clip(min=0)
        elif args['--function'] == 'Grad':
            resp = ((img - np.roll(img, 1, axis=0)).clip(min=0))**2 + ((img - np.roll(img, 1, axis=1)).clip(min=0))**2
        elif args['--function'] == 'Sobel':
            resp = skimage.filters.sobel(img).clip(min=0)
        else:
            log.error('Function name: \'{}\' is unknown!'.format(args['--function']))
            sys.exit(1)
        resp[crop_mask] = np.NaN
        images['response'] = resp


        # tolerance is max distance between actual star position and expected star position
        # this should be a little smaller than 1° because this is the minimum distance
        # between 2 catalogue stars (catalogue was filtered for this)
        tolerance = np.max([0, int((float(config['image']['radius'])/90-1)/2)])
        log.debug('Calculate Filter response')
        
        # calculate x and y position where response has its max value (search within 'tolerance' range)
        stars = pd.concat([stars.drop(['maxX','maxY'], errors='ignore', axis=1), stars.apply(
                lambda s : findLocalMaxPos(resp, s.x, s.y, tolerance),
                axis=1)], axis=1
        )

        # drop stars that got mistaken for a brighter neighboor
        stars = stars.sort_values('vmag').drop_duplicates(subset=['maxX', 'maxY'], keep='first')

        # calculate response and drop stars that were not found at all, because response=0 interferes with log-plot
        stars['response'] = stars.apply(lambda s : findLocalMaxValue(resp, s.x, s.y, tolerance), axis=1)
        #stars['response_mean'] = stars.apply(lambda s : findLocalMean(resp, s.x, s.y, tolerance*2), axis=1)
        #stars['response_std'] = stars.apply(lambda s : findLocalStd(resp, s.x, s.y, tolerance*2), axis=1)
        stars.query('response > 1e-100', inplace=True)

        # correct atmospherice absorbtion
        lim = split('\\s*,\\s*', config['calibration']['airmass_absorbtion'])
        stars['response_orig'] = stars.response
        stars['response'] = stars.response / transmission3(stars.altitude, 1.0, float(lim[0]))
        
        if args['--function'] == 'All' or args['--ratescan']:
            stars['response_grad'] = stars.apply(lambda s : findLocalMaxValue(grad, s.x, s.y, tolerance), axis=1)
            stars['response_sobel'] = stars.apply(lambda s : findLocalMaxValue(sobel, s.x, s.y, tolerance), axis=1)
        lim = (split('\\s*,\\s*', config['analysis']['visibleupperlimit']), split('\\s*,\\s*', config['analysis']['visiblelowerlimit']))

        # calculate visibility percentage
        # if response > visibleUpperLimit -> visible=1
        # if response < visibleUpperLimit -> visible=0
        # if in between: scale linear
        stars['visible'] = np.minimum(
                1,
                np.maximum(
                    0,
                    (np.log10(stars['response']) - (stars['vmag']*float(lim[1][0]) + float(lim[1][1]))) / 
                    ((stars['vmag']*float(lim[0][0]) + float(lim[0][1])) - (stars['vmag']*float(lim[1][0]) + float(lim[1][1])))
                    )
                )
        #stars.loc[stars.response_std/stars.response_mean > 1.5, 'visible'] = 0
        # set visible = 0 for all magnitudes where upperLimit < lowerLimit
        stars.loc[stars.vmag.values > (float(lim[1][1]) - float(lim[0][1])) / (float(lim[0][0]) - float(lim[1][0])), 'visible'] = 0

        #stars['blobSize'] = stars.apply(lambda s : getBlobsize(resp[s.maxY-25:s.maxY+26, s.maxX-25:s.maxX+26], s.response*0.1), axis=1)

        # append results
        kernelResults.append(stars)
    del stars
    try:
        del stars_orig
    except UnboundLocalError:
        pass

    # merge all stars (if neccessary)
    try:
        celObjects['stars'] = pd.concat(kernelResults, keys=kernelSize)
    except ValueError:
        celObjects['stars'] = kernelResults[0]

    # use 'stars' as substitution because it is shorter
    celObjects['stars'].reset_index(0, drop=True, inplace=True)
    stars = celObjects['stars']

    if len(kernelSize) == 1:
        celObjects['points_of_interest']['starPercentage'] = celObjects['points_of_interest'].apply(
                lambda p,stars=stars : calc_star_percentage(p, stars, p.radius, unit='deg', lim=-1, weight=True),
                axis=1)
    else:
        log.warning('Can not process points_of_interest if multiple kernel sizes get used')
    output['global_star_perc'] = calc_star_percentage({'altitude': np.pi/2, 'azimuth':0}, stars, float(config['image']['openingangle']), unit='deg', lim=-1, weight=True)
    
    
    ##################################

    if args['--cam'] or args['--daemon']:
        output['img'] = img
        fig = plt.figure(figsize=(16,9))
        vmin = np.nanpercentile(img, 5)
        vmax = np.nanpercentile(img, 90.)
        plt.imshow(img, vmin=vmin,vmax=vmax, cmap='gray')
        stars.plot.scatter(x='x',y='y', ax=plt.gca(), c='visible', cmap = plt.cm.RdYlGn, s=30, vmin=0, vmax=1, grid=True)
        celObjects['points_of_interest'].plot.scatter(x='x', y='y', ax=plt.gca(), s=80, color='white', marker='^', label='Sources')
        plt.colorbar()
        plt.tight_layout()

        if args['-s']:
            plt.savefig('cam_image_{}.pdf'.format(images['timestamp'].isoformat()))
        if args['--daemon']:
            plt.savefig('cam_image_{}.png'.format(config['properties']['name']),dpi=300)
        if args['-v']:
            plt.show()
        plt.close('all')

    if args['--single'] or args['--daemon']:
        if args['--response'] or args['--daemon']:
            fig = plt.figure(figsize=(16,9))
            ax = plt.subplot(111)
            ax.semilogy()

            # draw visibility limits
            x = np.linspace(-5+stars.vmag.min(), stars.vmag.max()+5, 20)
            lim = (list(map(float, split('\\s*,\\s*', config['analysis']['visibleupperlimit']))), 
                    list(map(float, split('\\s*,\\s*', config['analysis']['visiblelowerlimit']))))
            y1 = 10**(x*lim[1][0] + lim[1][1])
            y2 = 10**(x*lim[0][0] + lim[0][1])
            ax.plot(x, y1, c='red', label='lower limit')
            ax.plot(x, y2, c='green', label='upper limit')

            stars.plot.scatter(x='vmag', y='response', ax=ax, logy=True, c=stars.visible.values,
                    cmap = plt.cm.RdYlGn, grid=True, vmin=0, vmax=1, label='Kernel Response')
            ax.set_xlim((-1, max(stars['vmag'])+0.5))
            ax.set_ylim((10**(lim[1][1]-1),10**(lim[0][1]+1)))
            ax.set_ylabel('Kernel Response')
            ax.set_xlabel('Star Magnitude')
            if args['-c'] == 'GTC':
                if args['--function'] == 'Grad':
                    ax.axhspan(ymin=11**2/255**2, ymax=13**2/255**2, color='red', alpha=0.5, label='old threshold range')
                ax.axvline(4.5, color='black', label='Magnitude lower limit')

            # show camera image in a subplot
            ax_in= inset_axes(ax,
                    width='30%',
                    height='40%',
                    loc=3)
            vmin = np.nanpercentile(img, 0.5)
            vmax = np.nanpercentile(img, 99.)
            ax_in.imshow(img,cmap='gray',vmin=vmin,vmax=vmax)
            color = cm.RdYlGn(stars.visible.values)
            stars.plot.scatter(x='x',y='y', ax=ax_in, c=color, vmin=0, vmax=1, grid=True)
            ax_in.get_xaxis().set_visible(False)
            ax_in.get_yaxis().set_visible(False)
            
            ax.legend(loc='best')
            if args['-s']:
                plt.savefig('response_{}_{}.png'.format(args['--function'], images['timestamp'].isoformat()))
            if args['--daemon']:
                plt.savefig('response_{}.png'.format(config['properties']['name']),dpi=200)
            if args['-v']:
                plt.show()
            plt.close('all')

        if args['--ratescan']:
            log.info('Doing ratescan')
            gradList = list()
            sobelList = list()
            lapList = list()

            response = np.logspace(-4.5,-0.5,200)
            for resp in response:
                labeled, labelCnt = label(grad>resp)
                stars['visible'] = stars.response_grad > resp
                gradList.append((calc_star_percentage(0, stars, -1), np.sum(grad > resp), labelCnt, sum(stars.visible)))
                labeled, labelCnt = label(sobel>resp)
                stars['visible'] = stars.response_sobel > resp
                sobelList.append((calc_star_percentage(0, stars, -1), np.sum(sobel > resp), labelCnt, sum(stars.visible)))
                labeled, labelCnt = label(lap>resp)
                stars['visible'] = stars.response > resp
                lapList.append((calc_star_percentage(0, stars, -1), np.sum(lap > resp), labelCnt, sum(stars.visible)))

            gradList = np.array(gradList)
            sobelList = np.array(sobelList)
            lapList = np.array(lapList)

            #minThresholds = [max(response[l[:,0]==1]) for l in (gradList, sobelList, lapList)]

            minThresholds = -np.array([np.argmax(gradList[::-1,0]), np.argmax(sobelList[::-1,0]), np.argmax(lapList[::-1,0])]) + len(response) -1
            clusters = (gradList[minThresholds[0],2], sobelList[minThresholds[1],2], lapList[minThresholds[2],2])
            thresh = (response[minThresholds[0]], response[minThresholds[1]],response[minThresholds[2]])
            fig = plt.figure(figsize=(19.2,10.8))
            ax1 = fig.add_subplot(111)
            plt.xscale('log')
            plt.grid()
            ax1.plot(response, sobelList[:,0], marker='x', c='blue', label='Sobel Kernel - Percent')
            ax1.plot(response, lapList[:,0], marker='x', c='red', label='LoG Kernel - Percent')
            ax1.plot(response, gradList[:,0], marker='x', c='green', label='Square Gradient - Percent')
            ax1.axvline(response[minThresholds[0]], color='green')
            ax1.axvline(response[minThresholds[1]], color='blue')
            ax1.axvline(response[minThresholds[2]], color='red')
            ax1.axvline(14**2/255**2, color='black', label='old threshold')
            ax1.set_ylabel('')
            ax1.legend(loc='center left')

            ax2 = ax1.twinx()
            #ax2.plot(response, gradList[:,1], marker='o', c='green', label='Square Gradient - Pixcount')
            #ax2.plot(response, sobelList[:,1], marker='o', c='blue', label='Sobel Kernel - Pixcount')
            #ax2.plot(response, lapList[:,1], marker='o', c='red', label='LoG Kernel - Pixcount')
            ax2.plot(response, gradList[:,2], marker='s', c='green', label='Square Gradient - Clustercount')
            ax2.plot(response, sobelList[:,2], marker='s', c='blue', label='Sobel Kernel - Clustercount')
            ax2.plot(response, lapList[:,2], marker='s', c='red', label='LoG Kernel - Clustercount')
            ax2.axhline(gradList[minThresholds[0],2], color='green')
            ax2.axhline(sobelList[minThresholds[1],2], color='blue')
            ax2.axhline(lapList[minThresholds[2],2], color='red')
            ax2.legend(loc='upper right')
            ax2.set_xlim((min(response), max(response)))
            ax2.set_ylim((0,16000))
            if args['-v']:
                plt.show()
            if args['-s']:
                plt.savefig('rateScan.pdf')
            plt.close('all')

            output['response'] = response
            output['thresh'] = thresh
            output['minThresh'] = minThresholds
            del grad
            del sobel
            del lap

    if args['--cloudmap'] or args['--cloudtrack'] or args['--daemon']:
        log.debug('Calculating cloud map')
        cloud_map = calc_cloud_map(stars, img.shape[1]//80, img.shape, weight=True)
        cloud_map[crop_mask] = 1
        if args['--cloudtrack']:
            output['cloudmap'] = cloud_map
        if args['--cloudmap']:
            ax1 = plt.subplot(121)
            vmin = np.nanpercentile(img, 5.5)
            vmax = np.nanpercentile(img, 99.9)
            ax1.imshow(img, vmin=vmin, vmax=vmax, cmap='gray', interpolation='none')
            ax1.grid()

            ax2 = plt.subplot(122)
            ax2.imshow(cloud_map, cmap='gray_r', vmin=0, vmax=1)
            ax2.grid()
            if args['-s']:
                plt.savefig('cloudMap_{}.png'.format(images['timestamp'].isoformat()))
            if args['-v']:
                plt.show()
            plt.close('all')
        if args['--daemon']:
            ax = plt.subplot(111)
            ax.imshow(cloud_map, cmap='gray_r', vmin=0, vmax=1)
            ax.grid()
            plt.savefig('cloudMap_{}.png'.format(config['properties']['name']),dpi=200)
    try:
        output['global_coverage'] = np.nanmean(cloudmap)
    except NameError:
        log.warning('Cloudmap not available. Calculating global_coverage not possible')
        output['global_coverage'] = np.float64(-1)

    del images
    output['stars'] = stars
    output['points_of_interest'] = celObjects['points_of_interest']
    output['sun_alt'] = celObjects['sun']['altitude']
    output['moon_alt'] = celObjects['moon']['altitude']
    output['moon_phase'] = celObjects['moon']['moonPhase']

    if args['--sql']:
        try:
            sql.writeSQL(config, output)
        except (OperationalError):
            log.error('Writing to SQL server failed. Server up? Password correct?')
        except InternalError as e:
            log.error('Error while writing to SQL server: {}'.format(e))


    if args['--low-memory']:
        slimOutput = dict()
        for key in ['timestamp', 'hash', 'points_of_interest', 'sun_alt', 'moon_alt', 'moon_phase', 'brightness_mean', 'brightness_std']:
            try:
                slimOutput[key] = [output[key]]
            except KeyError:
                log.warning('Key {} was not found in dataframe so it can not be returned/stored'.format(key))
        del output
        output = slimOutput
        del slimOutput
                
    if args['--daemon']:
        del output
        output = None

    log.info('Done')
    return output
