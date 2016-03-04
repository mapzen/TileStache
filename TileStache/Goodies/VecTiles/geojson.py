from re import compile
from math import pi, log, tan, ceil

import json

from shapely.wkb import loads
from shapely.geometry import asShape

from .ops import transform

float_pat = compile(r'^-?\d+\.\d+(e-?\d+)?$')
charfloat_pat = compile(r'^[\[,\,]-?\d+\.\d+(e-?\d+)?$')

# floating point lat/lon precision for each zoom level, good to ~1/4 pixel.
precisions = [int(ceil(log(1<<zoom + 8+2) / log(10)) - 2) for zoom in range(17)]
# at z16, need to be more precise for metatiling
precisions[16] = 8

def mercator((x, y)):
    ''' Project an (x, y) tuple to spherical mercator.
    '''
    x, y = pi * x/180, pi * y/180
    y = log(tan(0.25 * pi + 0.5 * y))
    return 6378137 * x, 6378137 * y

def decode(file):
    ''' Decode a GeoJSON file into a list of (WKB, property dict) features.
    
        Result can be passed directly to mapnik.PythonDatasource.wkb_features().
    '''
    data = json.load(file)
    features = []
    
    for feature in data['features']:
        if feature['type'] != 'Feature':
            continue
        
        if feature['geometry']['type'] == 'GeometryCollection':
            continue
        
        prop = feature['properties']
        geom = transform(asShape(feature['geometry']), mercator)
        features.append((geom.wkb, prop))
    
    return features

def encode(file, features, zoom):
    ''' Encode a list of (WKB, property dict, id) features into a GeoJSON stream.

        If no id is available, pass in None

        Geometries in the features list are assumed to be unprojected lon, lats.
        Floating point precision in the output is truncated to six digits.
    '''
    fs = []
    for feature in features:
        assert len(feature) == 3
        wkb, props, fid = feature
        f = dict(type='Feature', properties=props,
                 geometry=loads(wkb).__geo_interface__)
        if fid is not None:
            f['id'] = fid
        fs.append(f)

    geojson = dict(type='FeatureCollection', features=fs)

    write_to_file(file, geojson, zoom)

def merge(file, names, tiles, config, coord):
    ''' Retrieve a list of GeoJSON tile responses and merge them into one.

        get_tiles() retrieves data and performs basic integrity checks.
    '''
    output = dict(zip(names, tiles))
    write_to_file(file, output, coord.zoom)

def write_to_file(file, geojson, zoom):
    ''' Write GeoJSON stream to a file

    '''
    encoder = json.JSONEncoder(separators=(',', ':'))
    encoded = encoder.iterencode(geojson)
    precision_idx = zoom if 0 <= zoom < len(precisions) else -1
    flt_fmt = '%%.%df' % precisions[precision_idx]

    for token in encoded:
        if charfloat_pat.match(token):
            # in python 2.7, we see a character followed by a float literal
            file.write(token[0] + flt_fmt % float(token[1:]))
        
        elif float_pat.match(token):
            # in python 2.6, we see a simple float literal
            file.write(flt_fmt % float(token))
        
        else:
            file.write(token)
