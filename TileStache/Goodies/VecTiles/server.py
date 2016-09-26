''' Provider that returns PostGIS vector tiles in GeoJSON or MVT format.

VecTiles is intended for rendering, and returns tiles with contents simplified,
precision reduced and often clipped.

For a more general implementation, try the Vector provider:
    http://tilestache.org/doc/#vector-provider
'''
from math import pi
from urlparse import urljoin, urlparse
from urllib import urlopen
from os.path import exists
from shapely.wkb import dumps
from shapely.wkb import loads

import json
from ... import getTile
from ...Core import KnownUnknown
from TileStache.Config import loadClassPath

try:
    from psycopg2.extras import RealDictCursor
    from psycopg2 import connect
    from psycopg2.extensions import TransactionRollbackError

except ImportError, err:
    # Still possible to build the documentation without psycopg2

    def connect(*args, **kwargs):
        raise err

from . import mvt, geojson, topojson, oscimap
from ...Geography import SphericalMercator
from ModestMaps.Core import Point

tolerances = [6378137 * 2 * pi / (2 ** (zoom + 8)) for zoom in range(22)]


def make_transform_fn(transform_fns):
    if not transform_fns:
        return None

    def transform_fn(shape, properties, fid, zoom):
        for fn in transform_fns:
            shape, properties, fid = fn(shape, properties, fid, zoom)
        return shape, properties, fid
    return transform_fn


def resolve_transform_fns(fn_dotted_names):
    if not fn_dotted_names:
        return None
    return map(loadClassPath, fn_dotted_names)


class Provider:
    ''' VecTiles provider for PostGIS data sources.
    
        Parameters:
        
          dbinfo:
            Required dictionary of Postgres connection parameters. Should
            include some combination of 'host', 'user', 'password', and 'database'.
        
          queries:
            Required list of Postgres queries, one for each zoom level. The
            last query in the list is repeated for higher zoom levels, and null
            queries indicate an empty response.
            
            Query must use "__geometry__" for a column name, and must be in
            spherical mercator (900913) projection. A query may include an
            "__id__" column, which will be used as a feature ID in GeoJSON
            instead of a dynamically-generated hash of the geometry. A query
            can additionally be a file name or URL, interpreted relative to
            the location of the TileStache config file.
            
            If the query contains the token "!bbox!", it will be replaced with
            a constant bounding box geomtry like this:
            "ST_SetSRID(ST_MakeBox2D(ST_MakePoint(x, y), ST_MakePoint(x, y)), <srid>)"
            
            This behavior is modeled on Mapnik's similar bbox token feature:
            https://github.com/mapnik/mapnik/wiki/PostGIS#bbox-token
          
          clip:
            Optional boolean flag determines whether geometries are clipped to
            tile boundaries or returned in full. Default true: clip geometries.
        
          srid:
            Optional numeric SRID used by PostGIS for spherical mercator.
            Default 900913.
        
          simplify:
            Optional floating point number of pixels to simplify all geometries.
            Useful for creating double resolution (retina) tiles set to 0.5, or
            set to 0.0 to prevent any simplification. Default 1.0.
        
          simplify_until:
            Optional integer specifying a zoom level where no more geometry
            simplification should occur. Default 16.

          suppress_simplification:
            Optional list of zoom levels where no dynamic simplification should
            occur.

          geometry_types:
            Optional list of geometry types that constrains the results of what
            kind of features are returned.

          transform_fns:
            Optional list of transformation functions. It will be
            passed a shapely object, the properties dictionary, and
            the feature id. The function should return a tuple
            consisting of the new shapely object, properties
            dictionary, and feature id for the feature.

          sort_fn:
            Optional function that will be used to sort features
            fetched from the database.

        Sample configuration, for a layer with no results at zooms 0-9, basic
        selection of lines with names and highway tags for zoom 10, a remote
        URL containing a query for zoom 11, and a local file for zooms 12+:

          "provider":
          {
            "class": "TileStache.Goodies.VecTiles:Provider",
            "kwargs":
            {
              "dbinfo":
              {
                "host": "localhost",
                "user": "gis",
                "password": "gis",
                "database": "gis"
              },
              "queries":
              [
                null, null, null, null, null,
                null, null, null, null, null,
                "SELECT way AS __geometry__, highway, name FROM planet_osm_line -- zoom 10+ ",
                "http://example.com/query-z11.pgsql",
                "query-z12-plus.pgsql"
              ]
            }
          }
    '''
    def __init__(self, layer, dbinfo, queries, clip=True, srid=900913, simplify=1.0, simplify_until=16, suppress_simplification=(), geometry_types=None, transform_fns=None, sort_fn=None, simplify_before_intersect=False):
        '''
        '''
        self.layer = layer

        keys = 'host', 'user', 'password', 'database', 'port', 'dbname'
        self.dbinfo = dict([(k, v) for (k, v) in dbinfo.items() if k in keys])

        self.clip = bool(clip)
        self.srid = int(srid)
        self.simplify = float(simplify)
        self.simplify_until = int(simplify_until)
        self.suppress_simplification = set(suppress_simplification)
        self.geometry_types = None if geometry_types is None else set(geometry_types)
        self.transform_fn_names = transform_fns
        self.transform_fn = make_transform_fn(resolve_transform_fns(transform_fns))
        if sort_fn:
            self.sort_fn_name = sort_fn
            self.sort_fn = loadClassPath(sort_fn)
        else:
            self.sort_fn_name = None
            self.sort_fn = None
        self.simplify_before_intersect = simplify_before_intersect

        self.queries = []
        self.columns = {}

        for query in queries:
            if query is None:
                self.queries.append(None)
                continue
        
            #
            # might be a file or URL?
            #
            url = urljoin(layer.config.dirpath, query)
            scheme, h, path, p, q, f = urlparse(url)
            
            if scheme in ('file', '') and exists(path):
                query = open(path).read()
            
            elif scheme == 'http' and ' ' not in url:
                query = urlopen(url).read()
        
            self.queries.append(query)
        
    def renderTile(self, width, height, srs, coord):
        ''' Render a single tile, return a Response instance.
        '''
        try:
            query = self.queries[coord.zoom]
        except IndexError:
            query = self.queries[-1]

        ll = self.layer.projection.coordinateProj(coord.down())
        ur = self.layer.projection.coordinateProj(coord.right())
        bounds = ll.x, ll.y, ur.x, ur.y
        
        if not query:
            return EmptyResponse(bounds)
        
        if query not in self.columns:
            self.columns[query] = query_columns(self.dbinfo, self.srid, query, bounds)
        
        if coord.zoom in self.suppress_simplification:
            tolerance = None
        else:
            tolerance = self.simplify * tolerances[coord.zoom] if coord.zoom < self.simplify_until else None

        return Response(self.dbinfo, self.srid, query, self.columns[query], bounds, tolerance, coord.zoom, self.clip, coord, self.layer.name(), self.geometry_types, self.transform_fn, self.sort_fn, self.simplify_before_intersect)

    def getTypeByExtension(self, extension):
        ''' Get mime-type and format by file extension, one of "mvt", "json" or "topojson".
        '''
        if extension.lower() == 'mvt':
            return 'application/x-protobuf', 'MVT'
        
        elif extension.lower() == 'json':
            return 'application/json', 'JSON'
        
        elif extension.lower() == 'topojson':
            return 'application/json', 'TopoJSON'

        elif extension.lower() == 'vtm':
            return 'image/png', 'OpenScienceMap' # TODO: make this proper stream type, app only seems to work with png

        else:
            raise ValueError(extension + " is not a valid extension")

class MultiProvider:
    ''' VecTiles provider to gather PostGIS tiles into a single multi-response.

        Returns a MultiResponse object for GeoJSON or TopoJSON requests.

        names:
          List of names of vector-generating layers from elsewhere in config.

        ignore_cached_sublayers:
          True if cache provider should not save intermediate layers
          in cache.

        Sample configuration, for a layer with combined data from water
        and land areas, both assumed to be vector-returning layers:

          "provider":
          {
            "class": "TileStache.Goodies.VecTiles:MultiProvider",
            "kwargs":
            {
              "names": ["water-areas", "land-areas"]
            }
          }
    '''
    def __init__(self, layer, names, ignore_cached_sublayers=False):
        self.layer = layer
        self.names = names
        self.ignore_cached_sublayers = ignore_cached_sublayers

    def __call__(self, layer, names, ignore_cached_sublayers=False):
        self.layer = layer
        self.names = names
        self.ignore_cached_sublayers = ignore_cached_sublayers

    def renderTile(self, width, height, srs, coord):
        ''' Render a single tile, return a Response instance.
        '''
        return MultiResponse(self.layer.config, self.names, coord, self.ignore_cached_sublayers)

    def getTypeByExtension(self, extension):
        ''' Get mime-type and format by file extension, "json" or "topojson" only.
        '''
        if extension.lower() == 'json':
            return 'application/json', 'JSON'
        
        elif extension.lower() == 'topojson':
            return 'application/json', 'TopoJSON'

        elif extension.lower() == 'vtm':
            return 'image/png', 'OpenScienceMap' # TODO: make this proper stream type, app only seems to work with png
        
        elif extension.lower() == 'mvt':
            return 'application/x-protobuf', 'MVT'

        else:
            raise ValueError(extension + " is not a valid extension for responses with multiple layers")

class Connection:
    ''' Context manager for Postgres connections.

        See http://www.python.org/dev/peps/pep-0343/
        and http://effbot.org/zone/python-with-statement.htm
    '''
    def __init__(self, dbinfo):
        self.dbinfo = dbinfo

    def __enter__(self):
        conn = connect(**self.dbinfo)
        conn.set_session(readonly=True, autocommit=True)
        self.db = conn.cursor(cursor_factory=RealDictCursor)
        return self.db

    def __exit__(self, type, value, traceback):
        self.db.connection.close()

class Response:
    '''
    '''
    def __init__(self, dbinfo, srid, subquery, columns, bounds, tolerance, zoom, clip, coord, layer_name, geometry_types, transform_fn, sort_fn, simplify_before_intersect):
        ''' Create a new response object with Postgres connection info and a query.

            bounds argument is a 4-tuple with (xmin, ymin, xmax, ymax).
        '''
        self.dbinfo = dbinfo
        self.bounds = bounds
        self.zoom = zoom
        self.clip = clip
        self.coord = coord
        self.layer_name = layer_name
        self.geometry_types = geometry_types
        self.transform_fn = transform_fn
        self.sort_fn = sort_fn

        geo_query = build_query(srid, subquery, columns, bounds, tolerance, True, clip, simplify_before_intersect=simplify_before_intersect)
        tol_idx = coord.zoom if 0 <= coord.zoom < len(tolerances) else -1
        tol_val = tolerances[tol_idx]
        oscimap_query = build_query(srid, subquery, columns, bounds, tolerance, False, clip, oscimap.padding * tol_val, oscimap.extents, simplify_before_intersect=simplify_before_intersect)
        mvt_query = build_query(srid, subquery, columns, bounds, tolerance, False, clip, mvt.padding * tol_val, mvt.extents, simplify_before_intersect=simplify_before_intersect)
        self.query = dict(TopoJSON=geo_query, JSON=geo_query, MVT=mvt_query, OpenScienceMap=oscimap_query)

    def save(self, out, format):
        '''
        '''
        features = get_features(self.dbinfo, self.query[format], self.geometry_types, self.transform_fn, self.sort_fn, self.coord.zoom)

        if format == 'MVT':
            mvt.encode(out, features, self.coord, self.bounds, self.layer_name)
        
        elif format == 'JSON':
            geojson.encode(out, features, self.zoom)
        
        elif format == 'TopoJSON':
            ll = SphericalMercator().projLocation(Point(*self.bounds[0:2]))
            ur = SphericalMercator().projLocation(Point(*self.bounds[2:4]))
            topojson.encode(out, features, (ll.lon, ll.lat, ur.lon, ur.lat))

        elif format == 'OpenScienceMap':
            oscimap.encode(out, features, self.coord, self.layer_name)

        else:
            raise ValueError(format + " is not supported")

class EmptyResponse:
    ''' Simple empty response renders valid MVT or GeoJSON with no features.
    '''
    def __init__(self, bounds):
        self.bounds = bounds
    
    def save(self, out, format):
        '''
        '''
        if format == 'MVT':
            mvt.encode(out, [], None)
        
        elif format == 'JSON':
            geojson.encode(out, [], 0)
        
        elif format == 'TopoJSON':
            ll = SphericalMercator().projLocation(Point(*self.bounds[0:2]))
            ur = SphericalMercator().projLocation(Point(*self.bounds[2:4]))
            topojson.encode(out, [], (ll.lon, ll.lat, ur.lon, ur.lat))

        elif format == 'OpenScienceMap':
            oscimap.encode(out, [], None)

        else:
            raise ValueError(format + " is not supported")

class MultiResponse:
    '''
    '''
    def __init__(self, config, names, coord, ignore_cached_sublayers):
        ''' Create a new response object with TileStache config and layer names.
        '''
        self.config = config
        self.names = names
        self.coord = coord
        self.ignore_cached_sublayers = ignore_cached_sublayers

    def save(self, out, format):
        '''
        '''
        if format == 'TopoJSON':
            topojson.merge(out, self.names, self.get_tiles(format), self.config, self.coord)
        
        elif format == 'JSON':
            geojson.merge(out, self.names, self.get_tiles(format), self.config, self.coord)

        elif format == 'OpenScienceMap':
            feature_layers = []
            layers = [self.config.layers[name] for name in self.names]
            for layer in layers:
                width, height = layer.dim, layer.dim
                tile = layer.provider.renderTile(width, height, layer.projection.srs, self.coord)
                if isinstance(tile,EmptyResponse): continue
                feature_layers.append({'name': layer.name(), 'features': get_features(tile.dbinfo, tile.query["OpenScienceMap"], layer.provider.geometry_types, layer.provider.transform_fn, layer.provider.sort_fn, self.coord.zoom)})
            oscimap.merge(out, feature_layers, self.coord)
        
        elif format == 'MVT':
            feature_layers = []
            layers = [self.config.layers[name] for name in self.names]
            for layer in layers:
                width, height = layer.dim, layer.dim
                tile = layer.provider.renderTile(width, height, layer.projection.srs, self.coord)
                if isinstance(tile,EmptyResponse): continue
                feature_layers.append({'name': layer.name(), 'features': get_features(tile.dbinfo, tile.query["MVT"], layer.provider.geometry_types, layer.provider.transform_fn, layer.provider.sort_fn, self.coord.zoom)})
            mvt.merge(out, feature_layers, self.coord)

        else:
            raise ValueError(format + " is not supported for responses with multiple layers")

    def get_tiles(self, format):
        unknown_layers = set(self.names) - set(self.config.layers.keys())
    
        if unknown_layers:
            raise KnownUnknown("%s.get_tiles didn't recognize %s when trying to load %s." % (__name__, ', '.join(unknown_layers), ', '.join(self.names)))
        
        layers = [self.config.layers[name] for name in self.names]
        mimes, bodies = zip(*[getTile(layer, self.coord, format.lower(), self.ignore_cached_sublayers, self.ignore_cached_sublayers) for layer in layers])
        bad_mimes = [(name, mime) for (mime, name) in zip(mimes, self.names) if not mime.endswith('/json')]
        
        if bad_mimes:
            raise KnownUnknown('%s.get_tiles encountered a non-JSON mime-type in %s sub-layer: "%s"' % ((__name__, ) + bad_mimes[0]))
        
        tiles = map(json.loads, bodies)
        bad_types = [(name, topo['type']) for (topo, name) in zip(tiles, self.names) if topo['type'] != ('FeatureCollection' if (format.lower()=='json') else 'Topology')]
        
        if bad_types:
            raise KnownUnknown('%s.get_tiles encountered a non-%sCollection type in %s sub-layer: "%s"' % ((__name__, ('Feature' if (format.lower()=='json') else 'Topology'), ) + bad_types[0]))
        
        return tiles


def query_columns(dbinfo, srid, subquery, bounds):
    ''' Get information about the columns returned for a subquery.
    '''
    with Connection(dbinfo) as db:
        bbox = 'ST_MakeBox2D(ST_MakePoint(%f, %f), ST_MakePoint(%f, %f))' % bounds
        bbox = 'ST_SetSRID(%s, %d)' % (bbox, srid)

        query = subquery.replace('!bbox!', bbox)

        # newline is important here, to break out of comments.
        db.execute(query + '\n LIMIT 0')
        column_names = set(x.name for x in db.description)
        return column_names


def get_features(dbinfo, query, geometry_types, transform_fn, sort_fn, zoom,
                 n_try=1):
    features = []

    with Connection(dbinfo) as db:
        try:
            db.execute(query)
        except TransactionRollbackError:
            if n_try >= 5:
                print 'TransactionRollbackError occurred 5 times'
                raise
            else:
                return get_features(dbinfo, query, geometry_types,
                                    transform_fn, sort_fn, zoom,
                                    n_try=n_try + 1)
        for row in db.fetchall():
            assert '__geometry__' in row, 'Missing __geometry__ in feature result'
            assert '__id__' in row, 'Missing __id__ in feature result'

            wkb = bytes(row.pop('__geometry__'))
            id = row.pop('__id__')

            shape = loads(wkb)
            if geometry_types is not None:
                if shape.type not in geometry_types:
                    #print 'found %s which is not in: %s' % (geom_type, geometry_types)
                    continue

            props = dict((k, v) for k, v in row.items() if v is not None)

            if transform_fn:
                shape, props, id = transform_fn(shape, props, id, zoom)
                wkb = dumps(shape)

            features.append((wkb, props, id))

    if sort_fn:
        features = sort_fn(features, zoom)

    return features

def build_query(srid, subquery, subcolumns, bounds, tolerance, is_geo, is_clipped, padding=0, scale=None, simplify_before_intersect=False):
    ''' Build and return an PostGIS query.
    '''

    # bounds argument is a 4-tuple with (xmin, ymin, xmax, ymax).
    bbox = 'ST_MakeBox2D(ST_MakePoint(%.12f, %.12f), ST_MakePoint(%.12f, %.12f))' % (bounds[0] - padding, bounds[1] - padding, bounds[2] + padding, bounds[3] + padding)
    bbox = 'ST_SetSRID(%s, %d)' % (bbox, srid)
    geom = 'q.__geometry__'
    
    # Special care must be taken when simplifying certain geometries (like those
    # in the earth/water layer) to prevent tile border "seams" from forming:
    # these occur when a geometry is split across multiple tiles (like a
    # continuous strip of land or body of water) and thus, for any such tile,
    # the part of that geometry inside of it lines up along one or more of its
    # edges. If there's any kind of fine geometric detail near one of these
    # edges, simplification might remove it in a way that makes the edge of the
    # geometry move off the edge of the tile. See this example of a tile
    # pre-simplification:
    # https://cloud.githubusercontent.com/assets/4467604/7937704/aef971b4-090f-11e5-91b9-d973ef98e5ef.png
    # and post-simplification:
    # https://cloud.githubusercontent.com/assets/4467604/7937705/b1129dc2-090f-11e5-9341-6893a6892a36.png
    # at which point a seam formed.
    #
    # To get around this, for any given tile bounding box, we find the
    # contained/overlapping geometries and simplify them BEFORE
    # cutting out the precise tile bounding bbox (instead of cutting out the
    # tile and then simplifying everything inside of it, as we do with all of
    # the other layers).

    if simplify_before_intersect:
        # Simplify, then cut tile.

        if tolerance is not None:
            # The problem with simplifying all contained/overlapping geometries
            # for a tile before cutting out the parts that actually lie inside
            # of it is that we might end up simplifying a massive geometry just
            # to extract a small portion of it (think simplifying the border of
            # the US just to extract the New York City coastline). To reduce the
            # performance hit, we actually identify all of the candidate
            # geometries, then cut out a bounding box *slightly larger* than the
            # tile bbox, THEN simplify, and only then cut out the tile itself.
            # This still allows us to perform simplification of the geometry
            # edges outside of the tile, which prevents any seams from forming
            # when we cut it out, but means that we don't have to simplify the
            # entire geometry (just the small bits lying right outside the
            # desired tile).

            simplification_padding = padding + (bounds[3] - bounds[1]) * 0.1
            simplification_bbox = (
                'ST_MakeBox2D(ST_MakePoint(%.12f, %.12f), '
                'ST_MakePoint(%.12f, %.12f))' % (
                    bounds[0] - simplification_padding,
                    bounds[1] - simplification_padding,
                    bounds[2] + simplification_padding,
                    bounds[3] + simplification_padding))
            simplification_bbox = 'ST_SetSrid(%s, %d)' % (
                simplification_bbox, srid)

            geom = 'ST_Intersection(%s, %s)' % (geom, simplification_bbox)
            geom = 'ST_MakeValid(ST_SimplifyPreserveTopology(%s, %.12f))' % (
                geom, tolerance)
    
        assert is_clipped, 'If simplify_before_intersect=True, ' \
            'is_clipped should be True as well'
        geom = 'ST_Intersection(%s, %s)' % (geom, bbox)

    else:
        # Cut tile, then simplify.

        if is_clipped:
            geom = 'ST_Intersection(%s, %s)' % (geom, bbox)

        if tolerance is not None:
            geom = 'ST_SimplifyPreserveTopology(%s, %.12f)' % (geom, tolerance)
    
    if is_geo:
        geom = 'ST_Transform(%s, 4326)' % geom

    if scale:
        # scale applies to the un-padded bounds, e.g. geometry in the padding area "spills over" past the scale range
        geom = ('ST_TransScale(%s, %.12f, %.12f, %.12f, %.12f)'
                % (geom, -bounds[0], -bounds[1],
                   scale / (bounds[2] - bounds[0]),
                   scale / (bounds[3] - bounds[1])))

    subquery = subquery.replace('!bbox!', bbox)
    columns = ['q."%s"' % c for c in subcolumns if c not in ('__geometry__', )]
    
    if '__geometry__' not in subcolumns:
        raise Exception("There's supposed to be a __geometry__ column.")
    
    if '__id__' not in subcolumns:
        columns.append('Substr(MD5(ST_AsBinary(q.__geometry__)), 1, 10) AS __id__')
    
    columns = ', '.join(columns)
    
    return '''SELECT %(columns)s,
                     ST_AsBinary(%(geom)s) AS __geometry__
              FROM (
                %(subquery)s
                ) AS q
              WHERE ST_IsValid(q.__geometry__)
                AND ST_Intersects(q.__geometry__, %(bbox)s)''' \
            % locals()
