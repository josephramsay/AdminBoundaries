#!/usr/bin/env python

'''
v.0.0.1

download_admin_bdys.py

Copyright 2011 Crown copyright (c)
Land Information New Zealand and the New Zealand Government.
All rights reserved

This program is released under the terms of the new BSD license. See the 
LICENSE file for more information.

Tests on Address class

Created on 09/11/2016

@author: jramsay

Notes
to fetch localities file need share created to \\prdassfps01\GISData\Electoral specific\Enrollment Services\Meshblock_Address_Report
to fetch meshblock data need sftp connection to 144.66.244.17/Meshblock_Custodianship 
without updated python >2.7.9 cant use paramiko (see commit history) use pexpect instead
database conn uses lds_bde user and modifed pg_hba allowing; local, lds_bde, linz_db, peer 

TODO
No | X | Desc
---+---+-----
1. | x | Change legacy database config to common attribute mapping
2. | x | Shift file to table mapping into config
3. | x | Enforce create/drop schema
4. |   | Consistent return types from db calls
5. |   | Validation framework
6. | x | Standardise logging, remove from config
'''
 
__version__ = 1.0

import os
import sys
import re
import json
import string
import getopt
import psycopg2
import smtplib
import collections
import socket

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import logging
import datetime

PYVER3 = sys.version_info > (3,)

#2 to 3 imports
if PYVER3:
	 import tkinter as TK
	 from tkinter.constants import RAISED,SUNKEN,BOTTOM,RIGHT,LEFT,END,X,Y,W,E,N,S,ACTIVE  
	 from configparser import SafeConfigParser
else:
	 import Tkinter as TK
	 from Tkconstants import RAISED,SUNKEN,BOTTOM,RIGHT,LEFT,END,X,Y,W,E,N,S,ACTIVE  
	 from ConfigParser import SafeConfigParser

from zipfile import ZipFile

#from paramiko import Transport, SFTPClient

# from twisted.internet import reactor
# from twisted.internet.defer import Deferred
# from twisted.conch.ssh.common import NS
# from twisted.conch.scripts.cftp import ClientOptions
# from twisted.conch.ssh.filetransfer import FileTransferClient
# from twisted.conch.client.connect import connect
# from twisted.conch.client.default import SSHUserAuthClient, verifyHostKey
# from twisted.conch.ssh.connection import SSHConnection
# from twisted.conch.ssh.channel import SSHChannel
# from twisted.python.log import startLogging, err

from subprocess import Popen,PIPE,check_output

import pexpect

from optparse import OptionParser

try:
	from osgeo import ogr, osr, gdal
except:
	try:
		import ogr, osr, gdal
	except Exception as e:
		raise Exception('ERROR: cannot find python OGR and GDAL modules. '+str(e))
		#sys.exit('ERROR: cannot find python OGR and GDAL modules')

version_num = int(gdal.VersionInfo('VERSION_NUM'))
if version_num < 1100000:
	raise Exception('ERROR: Python bindings of GDAL 1.10 or later required')
	#sys.exit('ERROR: Python bindings of GDAL 1.10 or later required')

# make sure gdal exceptions are not silent
gdal.UseExceptions()
#osr.UseExceptions()
ogr.UseExceptions()

logger = None

# Prefix for imported temp tables
PREFIX = 'temp_'
# Use the temp schema to create snapshots to test against (won't overwrite admin_bdys tables)
TEST = False
# Holds dataabse connection instances
SELECTION = {'ogr':None,'psy':None}
# Number of query attempts to make
DEPTH = 5
#Processing options
OPTS = [('1. Load - Copy AB files from Servers','load',0),
		('2. Transfer - Copy AB tables from import schema to final schema','transfer',2),
		('3. Reject - Drop import tables and quit','reject',3)]
#name of config file
CONFIG = 'download_admin_bdys.ini'
PROPS = '../scripts/dab.properties'
#PROPS = 'dab.properties'
HOST = socket.gethostname()

#max number of retries in recursive loop (insertshp in this case
MAX_RETRY = 10

ENC = 'utf-8-sig'
ASC = 'ascii'

if PYVER3:
	def is_nonstr_iter(v):
		if isinstance(v, str):
			return False
		return hasattr(v, '__iter__')
	dec = lambda d: d
	enc = lambda e: e
	diter = lambda d: d.items()
	unistr = str
else:
	def is_nonstr_iter(v):
		return hasattr(v, '__iter__')
	dec = lambda d,enc=ASC: d.decode(enc)
	enc = lambda e,enc=ASC: e.encode(enc)
	diter = lambda d: d.iteritems()
	unistr = unicode
	
def convenc(input):
    if isinstance(input, dict):
        return {convenc(k): convenc(v) for k,v in diter(input)}
    elif isinstance(input, list):
        return [convenc(element) for element in input]
    elif isinstance(input, unistr):
        return enc(input)
    else:
        return input
       
def setRetryDepth(depth):
	global DEPTH
	DEPTH = depth
	
def shift_geom ( geom ):
	'''translate geometry to 0-360 longitude space'''
	if geom is None:
		return
	count = geom.GetGeometryCount()
	if count > 0:
		for i in range( count ):
			shift_geom( geom.GetGeometryRef( i ) )
	else:
		for i in range( geom.GetPointCount() ):
			x, y, z = geom.GetPoint( i )
			if x < 0:
				x = x + 360
			elif x > 360:
				x = x - 360
			geom.SetPoint( i, x, y, z )
	return

def ring_is_clockwise(ring):
	'''check is geometry ring is clockwise'''
	total = 0
	i = 0
	point_count = ring.GetPointCount()
	pt1 = ring.GetPoint(i)
	pt2 = None
	for i in range(point_count-1):
		pt2 = ring.GetPoint(i+1)
		total += (pt2[0] - pt1[0]) * (pt2[1] + pt1[1])
		pt1 = pt2
	return (total >= 0)

def fix_esri_polyon(geom):
	'''this is required because of a bug in OGR http://trac.osgeo.org/gdal/ticket/5538'''
	polygons = []
	count = geom.GetGeometryCount()
	if count > 0:
		poly = None
		for i in range( count ):
			ring = geom.GetGeometryRef(i)
			if ring_is_clockwise(ring):
				poly = ogr.Geometry(ogr.wkbPolygon)
				poly.AddGeometry(ring)
				polygons.append(poly)
			else:
				poly.AddGeometry(ring)
	new_geom = None
	if  len(polygons) > 1:
		new_geom = ogr.Geometry(ogr.wkbMultiPolygon)
		for poly in polygons:
			new_geom.AddGeometry(poly)
	else:
		new_geom = polygons.pop()
	return new_geom

def setupLogging(lf='DEBUG',ll=logging.DEBUG,ff=1):
	formats = {1:'%(asctime)s - %(levelname)s - %(module)s %(lineno)d - %(message)s',
			   2:':: %(module)s %(lineno)d - %(message)s',
			   3:'%(asctime)s,%(message)s'}
	
	log = logging.getLogger(lf)
	log.setLevel(ll)
	
	#path = os.path.normpath(os.path.join(os.path.dirname(__file__), "../log/"))
	#if not os.path.exists(path):
	#	os.mkdir(path)
	df = os.path.join(os.path.dirname(__file__),lf.lower()+'.log')
	
	fh = logging.FileHandler(df,'w')
	fh.setLevel(logging.DEBUG)
	
	formatter = logging.Formatter(formats[ff])
	fh.setFormatter(formatter)
	log.addHandler(fh)
	
	return log
logger = setupLogging()

class DataValidator(object):
	#DRAFT
	def __init__(self,conf):
		self.conf = conf
		
	def validateSpatial(self):
		'''Validates using specific queries, spatial or otherwise eg select addressPointsWithinMeshblocks()'''
		for f in self.conf.validation_spatial:
			Processor.attempt(self.conf, f)
			
	def validateData(self):
		'''Validates the ref data itself, eg enforcing meshblock code length'''
		for f in self.conf.validation_data:
			Processor.attempt(self.conf, f)

class ColumnMapperError(Exception):pass
class ColumnMapper(object):
	'''Actions the list of column mappings defined in the conf file'''
	tfnc = 3
	ofsrf = "find_srid('{schema}','{table}','{geom}')"#for when o(riginal)srid varies
	map = {}
	dra = {'drop':'ALTER TABLE {schema}.{table} DROP COLUMN IF EXISTS {drop}',
		   'rename':'ALTER TABLE {schema}.{table} RENAME COLUMN {old} TO {new}',
		   'add':'ALTER TABLE {schema}.{table} ADD COLUMN {add} {type}',
		   'cast':'ALTER TABLE {schema}.{table} ALTER COLUMN {cast} SET DATA TYPE {type}',
		   'srid':'SELECT UpdateGeometrySRID(\'{schema}\',\'{table}\', \'{geom}\', {srid})',
		   'trans1':'UPDATE {schema}.{table} SET {geom} = ST_Transform({geom}::geometry,{srid}::integer)',
		   'trans2':'ALTER TABLE {schema}.{table} ALTER COLUMN {geom} TYPE geometry(MultiPolygon,{srid}) USING ST_Transform({geom}, {srid})',
		   'trans3':'ALTER TABLE {schema}.{table} ALTER COLUMN {geom} TYPE geometry(MultiPolygon,{srid}) USING ST_Transform(ST_SetSRID({geom},{osrid}), {srid})',
		   'primary':'ALTER TABLE {schema}.{table} ADD PRIMARY KEY ({primary})',
		   'shift':'UPDATE {schema}.{table} SET {geom} = ST_Shift_Longitude({geom}) where ST_XMin({geom}) < 0'
		  
	}
	
	def __init__(self,conf):
		'''Init and read the column mapping definitions'''
		self.schema = conf.database_schema
		for attr in conf.__dict__:
			m = re.search('(\w+)_colmap',attr)
			if m: self.map[m.group(1)] = json.loads(getattr(conf,attr))
			
	def flatten(self,lol):
		'''Flattens a nested list into 1D'''
		if not isinstance(lol,str) and isinstance(lol, collections.Iterable): return [a for i in lol for a in self.flatten(i)]
		else: return [lol]
		 
	def action(self,section,table,action):
		'''Generate queries from the column map'''
		if section in self.map and table in self.map[section]:
			if action == 'trans' and 'geom' in self.map[section][table] and 'srid' in self.map[section][table]:
				return self.flatten(self.formqry(action,section,table, ''))
			elif action == 'primary' and 'primary' in self.map[section][table]:
				return self.flatten(self.formqry(action,section,table, ''))
			elif action in self.map[section][table]:
				return self.flatten([self.formqry(action,section,table, sta) for sta in self.map[section][table][action]])
		return []
	
	def _getArgs(self,a):
		return a.values() if type(a) in (dict,) else a
		
	def formqry(self,action,section,table,args):
		'''Returns the formatted query string based on request type; drop, rename, add, cast and proj'''
		queries = []
		ptable = PREFIX+table
		if action == 'drop': queries.append(self.dra[action].format(schema=self.schema,table=ptable,drop=args))
		elif action == 'rename': queries.append(self.dra[action].format(schema=self.schema,table=ptable,old=args['old'],new=args['new']))
		elif action == 'add': queries.append(self.dra[action].format(schema=self.schema,table=ptable,add=args['add'],type=args['type']))
		elif action == 'cast': queries.append(self.dra[action].format(schema=self.schema,table=ptable,cast=args['cast'],type=args['type']))
		elif action == 'trans': 
			g = self.map[section][table]['geom']
			s = self.map[section][table]['srid'] #4167
			o = self.ofsrf.format(schema=self.schema,table=ptable,geom=g)
			if self.tfnc<3: queries.append(self.dra['srid'].format(schema=self.schema,table=ptable,geom=g,srid=s))
			queries.append(self.dra['trans{}'.format(self.tfnc)].format(schema=self.schema,table=ptable,geom=g,srid=s,osrid=o))
			queries.append(self.dra['shift'].format(schema=self.schema,table=ptable,geom=g))
		elif action == 'primary':
			p = self.map[section][table]['primary']
			queries.append(self.dra['primary'].format(schema=self.schema,table=ptable,primary=p))
		else: raise ColumnMapperError('Unrecognised query type specifier, use drop/add/rename/cast/proj')	
		return queries
		
	def _formqry(self,f,d):
		'''Maps variable arg list to format string'''
		return f.format(*d)

	def _replaceUnderScore(self,uscolname):
		'''Replace underscores in column names with spaces and quote the result'''
		return '"{}"'.format(uscolname.replace('_',' '))
		
class DBSelectionException(Exception):pass
class DB(object):
	def __init__(self,conf,drv):
		self.conf = conf
		self.d = None
		if drv == 'ogr':
			self.d = DatabaseConn_ogr(self.conf)
		elif drv == 'psy':
			self.d = DatabaseConn_psycopg2(self.conf)
		else: 
			raise DBSelectionException("Choose DB using 'ogr' or 'psy'")
		if self.d: self.d.connect()

	def get(self,q,rt=None):
		return self.d.execute_query(q,rt)
		
	def __enter__(self):
		return self
	
	def __exit__(self,exc_type=None, exc_val=None, exc_tb=None):
		self.d.disconnect()
			
class DatabaseConnectionException(Exception):pass
class DatabaseConn_psycopg2(object):
	def __init__(self,conf):
		self.conf = conf
		self.exe = None
		
	def connect(self):
		self.pconn = psycopg2.connect( \
			host=self.conf.database_host,\
			database=self.conf.database_name,\
			user=self.conf.database_user,\
			password=self.conf.database_password)
		self.pconn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
		self.pcur = self.pconn.cursor()
		
	def execute_query(self,q,rt=None):
		'''Execute query q and return success/failure determined by fail=any error except no results
		optional arg rt can be (s)tring or (b)oolean and (i)nt default
		'''
		logger.info('psyEQ '+q)
		res = True
		#if rt: rt = rt.lower()
		try:
			self.pcur.execute(q)
			if rt == 's': res = self.pcur.fetchone()
			elif rt == 'b': res = self.pcur.rowcount>0
			elif rt == 'i': res = self.pcur.rowcount
			else: res = self.pcur.rowcount or None
		except psycopg2.ProgrammingError as pe: 
			if hasattr(pe,'message') and 'no results to fetch' in pe.message: res = True
			if rt == 'e' and hasattr(pe,'pgerror') and 'does not exist' in pe.pgerror: res = True
			else: raise 
		except Exception as e: 
			raise DatabaseConnectionException('Database query error, {}'.format(e))
		#for now just check if row returned as success
		#return bool(isinstance(res,int) and res>0)
		return res
	
	def disconnect(self):
		self.pconn.commit()
		self.pcur.close()
		self.pconn.close()

class DatabaseConn_ogr(object):
	
	def __init__(self,conf):
		
		self.conf = conf
		self.exe = None
		self.pg_drv = ogr.GetDriverByName('PostgreSQL')
		if self.pg_drv is None:
			logger.fatal('Could not load the OGR PostgreSQL driver')
			raise Exception('Could not load the OGR PostgreSQL driver')
			#sys.exit(1)
		self.pg_uri = 'PG:dbname={} host={} port={} user={} password={}'
		self.pg_uri = self.pg_uri.format(conf.database_name,conf.database_host,conf.database_port,conf.database_user,conf.database_password)
		
		self.pg_ds = None
		
	def connect(self):
		try:
			if not self.pg_ds: 
				self.pg_ds = self.pg_drv.Open(self.pg_uri, update = 1)
				#if self.conf.database_rolename:
				#	self.pg_ds.ExecuteSQL("SET ROLE " + self.conf.database_rolename)
		except Exception as e:
			logger.fatal("Can't open PG output database: " + str(e))
			raise
			#sys.exit(1)
			
	def execute_query(self,q,rs=None):
		logger.info('ogrEQ '+q)
		#TODO. Provide return value selection
		return self.pg_ds.ExecuteSQL(q)
		   
	def disconnect(self):
		del self.pg_ds
						  
class ConfReader(object):
	'''Configuration Reader reads (and writes temp objects) in cp format'''
	TEMP = 'temp'
	#database to webapi; d1d1, t1t2, p1p3
	HostD2W = re.sub('\d{2}','0{}'.format('dtp'.index(HOST[0])+1),HOST) if HOST[0] in 'dtp' and re.search('\d{2}',HOST) else HOST
	#webapi to database; d1d1, t2t1, p3p1
	HostW2D = re.sub('\d{2}','01',HOST) if HOST[0] in 'dtp' and re.search('\d{2}',HOST) else HOST
	
	DEF = {	'database_host':HostW2D.split('.')[0],
			'user_link':'http://{}:8080/ab/'.format(HOST) }
	
	def __init__(self):
		self.path = os.path.dirname(__file__)
		self.config_file = os.path.join(self.path,CONFIG)
		self.props_file = os.path.join(self.path,PROPS)
		self.parser = SafeConfigParser()
		found = self.parser.read(self.config_file)
		if not found:
			raise Exception('Could not load config ' + self.config_file)
			#sys.exit('Could not load config ' + config_files[0] )
		
		for section in ('connection','meshblock','nzlocalities','database','layer','user'):
			for option in self.parser.options(section):
				optval = self.parser.get(section,option)
				setattr(self,'{}_{}'.format(section,option),optval if optval else self._fitg(section,option))
	
		logger.info('Starting DAB')
		
	def _fitg(self,section,option):
		'''calculated default config values'''
		return self.DEF['{}_{}'.format(section,option)]
		
		
	def save(self,name,data):
		'''Configparser save for interrupted processing jobs'''
		if not self.parser.has_section(self.TEMP): 
			self.parser.add_section(self.TEMP)
		self.parser.set(self.TEMP,name,json.dumps(data))
		with open(self.config_file, 'w') as configfile: self.parser.write(configfile)
		
	def read(self,name,flush=False):
		'''Configparser read for interrupted processing jobs, deletes requested entry after a read. Only reads TEMP section'''
		rv = ()
		if self.parser.has_section(self.TEMP) and self.parser.has_option(self.TEMP,name): 
			rv = json.loads(self.parser.get(self.TEMP,name))
			#if clean is set the data will be deleted after this read so delete the section/option to prevent attempted re-read
			if flush:
				self.parser.remove_option(self.TEMP,name)
				self.parser.remove_section(self.TEMP)
				with open(self.config_file, 'w') as configfile: self.parser.write(configfile)
		return rv
	
	def load(self):
		'''Simple JSON config file reader'''
		with open(self.props_file) as handle:
			return convenc(json.load(handle))
			#data = enc(handle.read().replace('\n',''))
			#return json.loads(data,encoding='ascii')

		
#VARS = ConfReader.load()		
	
class ProcessorException(Exception):pass
class Processor(object):
	
	def __init__(self,conf,db,cm,sf):
		self.conf = conf
		vars = self.conf.load()
		self.mbcc = vars['mbcc'] 
		self.f2t = vars['f2t']
		self.cq = vars['cq']
		self.db = db
		self.cm = cm
		self.driver = ogr.GetDriverByName('ESRI Shapefile')
		self.sftp = sf
		self.secname = type(self).__name__.lower()
		
	def _pktest(self,s,t):
		'''Check whether the table had a primary key already. ExecuteSQL returns layer if successful OR null on error/no-result'''
		q = "select * from information_schema.table_constraints \
			 where table_schema like '{s}' \
			 and table_name like '{t}' \
			 and constraint_type like 'PRIMARY KEY'".format(s=s,t=t)
		logger.debug('pQ2 {}'.format(q))
		return Processor.attempt(self.conf, q, driver_type='psy')

	def extract(self,file):
		'''Takes a zip path/filename input and returns the path/names of any unzipped files'''
		nl = []
		with ZipFile(file,'r') as h:
			nl = h.namelist()
			for fname in nl:
				h.extract(fname,path=os.path.dirname(file))
		return ['{}/{}'.format(getattr(self.conf,'{}_localpath'.format(self.secname)),n) for n in nl] 
	
	@classmethod
	def recent(cls,filelist,pattern='[a-zA-Z_]*(\d{8}).*'):
		'''Get the latest date labelled file from a list using the datestring in the filename as a key'''
		exfiles = {re.match(pattern,val.decode()).group(1):val for val in filelist if re.match(pattern,val.decode())} 
		return exfiles[max(exfiles)] if len(exfiles)>0 else ''
		
	def delete(self,file):
		'''Clean up unzipped shapefile'''
		#for file in path:
		p,f = os.path.split(file)
		ff,fx = os.path.splitext(f)
		for candidate in os.listdir(p):
			cf,cx = os.path.splitext(candidate)
			if re.match(ff,cf): os.remove(os.path.join(p,candidate))
			
	def query(self,schema,table,headers='',values='',op='insert'):
		'''Builds data query'''
		#h = ','.join([i.replace(' ','_') for i in headers]).lower() if is_nonstr_iter(headers) else headers
		h = ','.join(headers).lower() if is_nonstr_iter(headers) else headers
		v = "'{}'".format("','".join(values)) if is_nonstr_iter(values) else values
		#return self.cq[op].format(schema,table,h,v).replace('"','\'')
		return self.cq[op].format(schema,table,h,v)
	
	def layername(self,in_layer):
		'''Returns the name of the layer that inserting a shapefile would create'''
		in_name = in_layer.GetName()
		return self.f2t[in_name][0] if in_name in self.f2t else in_name
		
	def deletelyr(self,tname):
		#dlayer = self.db.pg_ds.GetLayerByName('{}.{}'.format(self.conf.database_schema,tname))
		#self.db.pg_ds.DeleteLayer(dlayer.GetName())
		self.db.pg_ds.DeleteLayer('{}.{}{}'.format(self.conf.database_schema,PREFIX,tname))
		
	def insertshp(self,in_layer,retry=0):
		if not in_layer: raise ProcessorException('Attempt to process Empty Datasource')
		in_name,out_name = None,None

		#options
		create_opts = ['GEOMETRY_NAME='+'geom']
		create_opts.append('SCHEMA=' + self.conf.database_schema)
		create_opts.append('OVERWRITE=' + 'yes')
		
		#create new layer
		try: 
			in_name = in_layer.GetName()
			out_name = PREFIX+self.f2t[in_name][0] if in_name in self.f2t else in_name
			out_srs = in_layer.GetSpatialRef()
			
			logger.debug('Inserting shapefile {}->{}[{}] - {}'.format(in_name,out_name,out_srs.AutoIdentifyEPSG(),create_opts))
			out_layer = self.db.pg_ds.CreateLayer(
				name = out_name,
				srs = out_srs,
				geom_type = ogr.wkbMultiPolygon,
				options = create_opts
			)
			#build layer fields
			logger.debug('Structuring table {}->{}'.format(in_name,out_name))
			in_ldef = in_layer.GetLayerDefn()
			for i in range(0, in_ldef.GetFieldCount()):
				in_fdef = in_ldef.GetFieldDefn(i)
				out_layer.CreateField(in_fdef)
				logger.debug('Add {}[{}]'.format(out_name,in_fdef.GetName()))
		except RuntimeError as r:
			#Version.rebuild(self.conf) If a problem occurs any previously created tables are deleted
			logger.warn('Error creating layer {}, drop and rebuild. {}'.format(out_name,r))
			q1 = 'drop table if exists {} cascade'.format(out_name)
			Processor.attempt(self.conf, q1, driver_type='psy')
			return self.insertshp(in_layer,retry+1) if retry<10 else None
		except Exception as e:
			logger.fatal('Can not create {} output table. {}'.format(out_name,e))
			raise
			#sys.exit(1)
			
		#insert features
		logger.debug('Populating table {}'.format(out_name))
		try:
			in_layer.ResetReading()
			in_feat = in_layer.GetNextFeature()
			out_ldef = out_layer.GetLayerDefn()
			while in_feat:
				out_feat = ogr.Feature(out_ldef)
				for i in range(0, out_ldef.GetFieldCount()):
					value = in_feat.GetField(i)
					if value is None:
						out_feat.UnsetField(out_ldef.GetFieldDefn(i).GetNameRef())
					else:
						out_feat.SetField(out_ldef.GetFieldDefn(i).GetNameRef(), value)
				geom = in_feat.GetGeometryRef()
				#1. fix_esri_polygon (no longer needed?)
				#geom = fix_esri_polyon(geom)
				#2. shift_geom
				if self.conf.layer_shift_geometry and out_srs.IsGeographic():
						shift_geom(geom)
				#3. always force, bugfix
				geom = ogr.ForceToMultiPolygon(geom)
				out_feat.SetGeometry(geom)
				out_layer.CreateFeature(out_feat)
				in_feat = in_layer.GetNextFeature()
			
		except Exception as e:
			logger.fatal('Can not populate {} output table. {}'.format(out_name,e))
			raise 
			#sys.exit(1)
		
		logger.debug('Returning table {}'.format(out_name))
		return out_name
			
	def insertcsv(self,mbfile):
		#TODO catch runtime errors
		ff = os.path.splitext(os.path.basename(mbfile))[0]
		logger.info('Inserting csv {}'.format(ff))
		#self.db.connect()
		#mb = '/home/jramsay/Downloads/Stats_MB_TA_WKT_20160415-NEW.zip'
		first = True
		# this is a hack while using temptables
		csvhead = self.f2t[ff]
		with open(mbfile,'r') as fh:
			for line in fh:
				line = line.strip().encode('ascii','ignore').decode(ENC) if PYVER3 else line.strip().decode(ENC)
				if first: 
					headers = [h.strip().lower() for h in line.split(',')]
					createheaders   = ','.join(['"{}" VARCHAR'.format(m) if m.find(' ')>0 else '{} VARCHAR'.format(m) for m in headers])
					storedheaders = ','.join(['"{}" VARCHAR'.format(m) if m.find(' ')>0 else '{} VARCHAR'.format(m) for m in csvhead[1]])
					insertheaders   = ','.join(['"{}"'.format(m) if m.find(' ')>0 else '{}'.format(m) for m in headers])
					findqry = self.query(self.conf.database_schema,PREFIX+csvhead[0],op='find')
					if not self._attempt(findqry) or self._attempt(findqry).GetNextFeature().GetFieldAsInteger(0) == 0:
						#if import table doesnt exist, create it
						if storedheaders != createheaders: logger.warn('Unexpected table column names, {}'.format(createheaders))
						createqry = self.query(self.conf.database_schema,PREFIX+csvhead[0],createheaders,op='create')
						self._attempt(createqry)
					else:
						#otherwise truncate the existing table
						truncqry = self.query(self.conf.database_schema,PREFIX+csvhead[0],op='trunc')
						self._attempt(truncqry)
					first = False
				else:
					values = line.replace("'","''").split(',',len(headers)-1)
					#if int(values[0])<47800:continue
					if '"NULL"' in values: continue
					insertqry = self.query(self.conf.database_schema,PREFIX+csvhead[0],insertheaders,values,op='insert')
					self._attempt(insertqry)
		#self.db.disconnect()			
		return csvhead[0]
						   
	def mapcolumns(self,tablename):
		'''Perform input to final column mapping'''
		actions = ('add','drop','rename','cast','primary','trans')
		#primary key check only checks pk before structure changes, needs to check afterward
		#if self._pktest(self.conf.database_schema, PREFIX+tablename):
		#	actions = tuple(x for x in actions if x!='primary')
		for qlist in [self.cm.action(self.secname,tablename.lower(),adrc) for adrc in actions]: 
			for q in qlist: 
				if q and (q.find('ADD PRIMARY KEY')<0 or not self._pktest(self.conf.database_schema, PREFIX+tablename)):
					self._attempt(q)
					
	def assignperms(self,tablename):
		'''Give select-on-table and usage-on-schema for all named users'''
		for user in self.cm.map[self.secname][tablename]['permission']:
			self._attempt(self.cq['permit_t'].format(self.conf.database_schema,PREFIX+tablename,user))
			self._attempt(self.cq['permit_s'].format(self.conf.database_schema,user))
				
	def drop(self,table):
		'''Clean up any previous table instances. Doesn't work!''' 
		return self._attempt(self.cq['drop'].format(self.conf.database_schema,table))

	def _attempt(self,q):
		'''Wrapper for static attempt'''
		Processor.attempt(self.conf,q)
		
	@staticmethod
	def attempt(conf,q,driver_type='ogr',depth=0,rt=None,r=None):#,test=False):
		'''Attempt connection using ogr or psycopg drivers creating temp connection if conn object not stored'''
		while depth<DEPTH:
			try:
				logger.info('{} <- {}'.format(driver_type,q))
				#if using active DB instance 
				if SELECTION[driver_type]:
					return SELECTION[driver_type].execute_query(q,rt)
				#otherwise setup/delete temporary connection
				else:
					with DB(conf,driver_type) as conn:
						return conn.get(q,rt)
			except RuntimeError as r:
				logger.error('Attempt {} using {} failed, {}'.format(depth,driver_type,r))
				#if re.search('table_version.ver_apply_table_differences',q) and Processor.nonOGR(conf,q,depth-1): return
				rv = Processor.attempt(conf, q, Processor._next(driver_type), depth+1,r)
				logger.debug('Success {}'.format(rv))
				return rv
		if r: raise r
		
	@staticmethod
	def _next(s,slist=None):
		'''Get next db connector in selection array'''
		slist = slist or SELECTION
		return list(slist.keys())[(list(slist.keys()).index(s)+1)%len(slist)]
	
class Meshblock(Processor):
	'''Extract and process the meshblock, concordance and boundaries layers'''
	
	def __init__(self,conf,db,cm,sf):
		super(Meshblock,self).__init__(conf,db,cm,sf)   
			 
	def run(self):
		self.get()
		return self.secname,self.process()
		
	def get(self): 
		dfile = self.sftp.fetch(self.secname)
		#dfile='./Stats_Meshblock_concordance_20160607.zip'
		if re.search('\.zip$',dfile): 
			self.file = self.extract(dfile)
		else: self.file = [dfile,]
		
	def process(self,pathlist=None):
		tlist = ()
		#self.db.connect()
		ds = None
		if not pathlist: pathlist = [f for f in self.file if re.search('\.csv$|\.shp$',f)]
		#for every fine in the pathlist
		for mbfile in pathlist:
			#extract the shapefiles
			if re.match('.*\.shp$',mbfile):
				#self.mapcolumns(type(self).__name__.lower(),self.insertshp(self.driver.Open(mbfile,0).GetLayer(0))) #Gives OGR error!!! Assume unreferenced DS is GC'd?
				mbhandle = self.driver.Open(mbfile,0)
				mblayer = mbhandle.GetLayer(0)
				tname = self.layername(mblayer)
				#self.drop(tname) #this doesn't work for some reason
				#self.deletelyr(tname)
				self.insertshp(mblayer)
				self.mapcolumns(tname)
				self.assignperms(tname)
				tlist += (tname,)
				mbhandle.Destroy()				
			#extract the concordance csv
			elif re.match('.*\.csv$',mbfile):
				tname = self.insertcsv(mbfile)
				self.mapcolumns(tname)
				self.assignperms(tname)
				tlist += (tname,)
			
			self.delete(mbfile)
		#self.db.disconnect()
		return tlist
	 
class NZLocalities(Processor):
	'''Exract and process the nz_localities file'''
	#NB new format, see nz_locality
	
	def __init__(self,conf,db,cm,sf):
		super(NZLocalities,self).__init__(conf,db,cm,sf)
		
	def run(self):
		self.get()
		return self.secname,self.process()
		
	def get(self): 
		pass
	
	def process(self,pathlist=None):
		tlist = ()
		#self.db.connect()
		ds = None
		if not pathlist: pathlist = '{}{}.shp'.format(self.conf.nzlocalities_filepath,self.conf.nzlocalities_filename)
		ds = self.driver.Open(pathlist,0)
		if ds:
			nzlayer = ds.GetLayer(0)
			tname = self.layername(nzlayer)
			self.insertshp(nzlayer)
			self.mapcolumns(tname)
			self.assignperms(tname)
			tlist += (tname,)
			ds.Destroy()
		else:
			raise ProcessorException('Unable to initialise data source {}'.format(pathlist))
		#self.db.disconnect()
		return tlist
		
class Version(object):
	
	importfile = 'aimsref_import.sql'
	qtv = 'select table_version.ver_apply_table_differences({}, {}, {})'
	
	def __init__(self,conf,cm):
		self.conf = conf
		self.cm = cm
		
	def setup(self):
		'''Create temp schema'''
		self.teardown(self.conf)
		self.rebuild(self.conf)
			   
	@staticmethod	 
	def rebuild(conf):			  
		'''Drop and create import schema for fresh import'''		
		Version._wrapq(conf,'create schema {}'.format(conf.database_schema))
		
	@staticmethod
	def teardown(conf):
		'''drop temp schema'''
		Version._wrapq(conf,'drop schema if exists {} cascade'.format(conf.database_schema))
		
	@staticmethod
	def _wrapq(conf,q):
		'''Simple wrapper for running schema queries with a retry if error not raised'''
		depth = DEPTH
		while not Processor.attempt(conf, q, driver_type='psy'): 
			msg = '{} failed. {} attempts remaining'.format(q,depth)
			if depth: logger.warn(msg)
			else: raise Exception(msg)
			depth -= 1 
		
	def verdiffs(self,original,imported,pk):
		'''Get table_version diffs query string'''
		qct = "select table_version.ver_table_key_datatype('{}','{}')".format(original,pk);
		qvd = "select count(*) from table_version.ver_get_table_differences('{original}','{imported}','{pk}') as T(code char(1), id ###);".format(original=original,imported=imported,pk=pk)
		return qct,qvd
		
	def qset(self,original,imported,pk,dstr=None):
		'''Run table version and apply diffs'''
		q = ''
		dstr = dstr or datetime.datetime.now().isoformat()
		#table_version operations must be done in one active cursor
		q += "select table_version.ver_create_revision('DAB:{}');".format(dstr)
		q += "select table_version.ver_apply_table_differences('{original}','{imported}','{pk}');".format(original=original,imported=imported,pk=pk)
		q += "select table_version.ver_complete_revision();"
		return [q,]
	
	def detectdiffs(self,tablelist):		
		'''Identify diferences between final and interim versions of AB tables'''
		res = []
		for section in tablelist:
			sec, tab = section
			for t in tab:
				t2 = self.cm.map[sec][t]['table']
				pk = self.cm.map[sec][t]['primary']
				original = '{}.{}'.format(self.conf.database_originschema,t2)
				imported = '{}.{}{}'.format(self.conf.database_schema,PREFIX,t)
				qct,qvd = self.verdiffs(original,imported,pk)
				logger.debug('pQd1 {}\npQd2 {}'.format(qct,qvd))
				rct = Processor.attempt(self.conf,qct,driver_type='psy',rt='s')[0]
				#WORKAROUND. Calling table_version.ver_table_key_datatype on a serial returns None, interpret as int
				dif = int(Processor.attempt(self.conf,qvd.replace('###',rct if rct else 'int'),driver_type='psy',rt='s')[0])
				if dif>0: res += [(t2,dif),]
		return res
		
	def versiontables(self,tablelist):
		'''Build and execute the import queries for each import table'''
		for section in tablelist:
			sec, tab = section
			for t in tab:
				t2 = self.cm.map[sec][t]['table']
				pk = self.cm.map[sec][t]['primary']
				original = '{}.{}'.format(self.conf.database_originschema,t2)
				imported = '{}.{}{}'.format(self.conf.database_schema,PREFIX,t)
				for q in self.qset(original,imported,pk):
					logger.debug('pQ1 {}'.format(q))
					Processor.attempt(self.conf,q,driver_type='psy')
				self.gridtables(sec,t,t2)
					
	def gridtables(self,sec,tab,tname):
		'''Look for grid specification and grid the table if found'''
		if sec in self.cm.map and tab in self.cm.map[sec] and 'grid' in self.cm.map[sec][tab]:
			e = External(self.conf)
			e.build(tname,self.cm.map[sec][tab]['grid'])
		
class External(object):
	externals = (('table_grid.sql',"select public.create_table_polygon_grid('{schema}', '{table}', '{column}', {xres}, {yres})"),)
	
	def __init__(self,conf):
		self.conf = conf
		
		
	def build(self,gridtable,colres):
		'''Create temp schema'''
		#self.db.connect()
		for file,query in self.externals:
			schema,func = re.search('select ([a-zA-Z_\.]+)',query).group(1).split('.')
			if not self._fnctest(schema, func):
				with open(file,'r') as handle:
					text = handle.read()
				#self.db.pg_ds.ExecuteSQL(text)
				Processor.attempt(self.conf,text)
			col = colres['geocol']
			res = colres['res']
			dstschema = self.conf.database_schema if TEST else self.conf.database_originschema
			q = query.format(schema=dstschema, table=gridtable, column=col, xres=res, yres=res)
			logger.debug('eQ1 {}'.format(q))
			Processor.attempt(self.conf,q)
		#self.db.disconnect()
			
	def _fnctest(self,s,t):
		'''Check whether the table had a primary key already. ExecuteSQL returns layer if successful OR null on error/no-result'''
		q = "select * from information_schema.routines \
			 where routine_schema like '{s}' \
			 and routine_name like '{t}'".format(s=s,t=t)
		logger.debug('fQ2 {}'.format(q))
		return Processor.attempt(self.conf, q)
			
class PExpectException(Exception):pass
class PExpectSFTP(object):  
	  
	def __init__(self,conf):
		self.conf = conf
		self.target = '{}@{}:{}'.format(self.conf.connection_ftpuser,self.conf.connection_ftphost,self.conf.connection_ftppath)
		self.opts = ['-o','PasswordAuthentication=yes',self.target]
		self.prompt = 'sftp> '
		self.get_timeout = 60.0
		
	def fetch(self,dfile):
		'''Main fetch'''
		localpath = None
		sftp = pexpect.spawn('sftp',self.opts)
		#sftp.logfile = sys.stdout
		try:
			#while tomcat7 home is unwritable
			index = sftp.expect(['(?i)continue connecting (yes/no)?','(?i)password:'])
			if index == 0:
				sftp.sendline('yes')
				if sftp.expect('(?i)password:') == 0: 
					localpath = self.fetch2(sftp,dfile)
			elif index == 1:
				localpath = self.fetch2(sftp,dfile)
			else:
				raise PExpectException('Cannot initiate session using {}'.format(selt.opts))  
				
		except pexpect.EOF:
			raise PExpectException('End-Of-File received attempting connect')  
		except pexpect.TIMEOUT:
			raise PExpectException('Connection timeout occurred')  
		finally:
			sftp.sendline('bye')
			sftp.close()
			
		return localpath
	
	def fetch2(self,sftp,dfile):
		'''First inner pexpect script'''
		localpath,localfile = None,None
		filelist = []
		get_timeout = 60.0
		pattern = getattr(self.conf,'{}_filepattern'.format(dfile))
		sftp.sendline(self.conf.connection_ftppass)
		if sftp.expect(self.prompt) == 0:
			sftp.sendline('ls')
			if sftp.expect(self.prompt) == 0:
				for fname in sftp.before.split()[1:]:
					fmatch = re.match(pattern,fname.decode())
					if fmatch: filelist += [fname,]
				if filelist:
					fname = Processor.recent(filelist,pattern)
					localfile = re.match(pattern,fname.decode()).group(0)
				#break
				if not localfile: 
					raise PExpectException('Cannot find matching file pattern')
			else:
				raise PExpectException('Unable to access or empty directory at {}'.format(self.conf.connection_ftppath))
			localpath = '{}/{}'.format(getattr(self.conf,'{}_localpath'.format(dfile)),localfile)
			sftp.sendline('get {} {}'.format(localfile,localpath))
			if sftp.expect(self.prompt,self.get_timeout) != 0:
				raise PExpectException('Cannot retrieve file, {}/{}'.format(self.conf.connection_ftppath,localfile))
			#os.rename('./{}'.format(localfile),localpath)
		else: 
			raise PExpectException('Password authentication failed')  
		
		return localpath

class SimpleUI(object):
	'''Simple UI component added mainly to provide debian installer target, also used if run locally'''
	H = 100
	W = 100
	R = RAISED
	LAYOUT = '4x1' #2x2
	
	def __init__(self):
		self.master = TK.Tk()
		self.master.wm_title('DAB')
		self.mainframe = TK.Frame(self.master,height=self.H,width=self.W,bd=1,relief=self.R)
		self.mainframe.grid()
		self.initWidgets()
		self._offset(self.master)
		self.mainframe.mainloop()

	def initWidgets(self):
		title_row = 0
		select_row = 1
		button_row = select_row + int(max(list(re.sub("[^0-9]", "",self.LAYOUT))))
		
		#B U T T O N
		self.mainframe.selectbt = TK.Button(self.mainframe,  text='Start', command=self.start)
		self.mainframe.selectbt.grid( row=button_row,column=0,sticky=E)
 
		self.mainframe.quitbt = TK.Button(self.mainframe,	text='Quit',  command=self.quit)
		self.mainframe.quitbt.grid(row=button_row,column=1,sticky=E)
  
		#C H E C K B O X
		runlevel = TK.StringVar()
		runlevel.set('reject')
		for text,selection,col in OPTS:
			self.mainframe.rlev = TK.Radiobutton(self.mainframe, text=text, variable=runlevel, value=selection)#,indicatoron=False)
			if self.LAYOUT=='2x2':
				self.mainframe.rlev.grid(row=int(select_row+abs(col/2)),column=int(col%2),sticky=W)
			elif self.LAYOUT == '4x1':		 
				self.mainframe.rlev.grid(row=int(select_row+col),column=0,sticky=W)
		self.mainframe.rlev_var = runlevel   
		
		#L A B E L
		self.mainframe.title = TK.Label(self.mainframe,text='Select DAB Operation')
		self.mainframe.title.grid(row=title_row,column=0,sticky=W)
   
		
	def quit(self):
		'''Quit action'''
		self.ret_val = None 
		self.master.withdraw()
		self.mainframe.quit()
		
	def start(self):
		'''Start action, sets ret_val to cb selection'''
		self.ret_val = self.mainframe.rlev_var.get()
		self.master.withdraw()
		self.mainframe.quit()
		
	def _offset(self,window):
		'''Reposition window to centre of screen'''
		window.update_idletasks()
		w = window.winfo_screenwidth()
		h = window.winfo_screenheight()
		size = tuple(int(_) for _ in window.geometry().split('+')[0].split('x'))
		x = w/4 - size[0]/2
		y = h/4 - size[1]/2
		window.geometry("%dx%d+%d+%d" % (size + (x, y)))

	
def notify(c,dd=''):
	'''Send a notification email to the recipients list to inform that New Admin Boundary Data Is Available'''

	sender = 'no-reply@{}'.format(c.user_domain)
	recipients = ['{}@{}'.format(u,c.user_domain) for u in c.user_list.split(',')]

	try:
	# Create message container - the correct MIME type is multipart/alternative.
		msg = MIMEMultipart('alternative')
		msg['Subject'] = '*** New Admin Boundary Data Is Available ***'
		msg['From'] = sender
		msg['To'] = ', '.join(recipients)
	
	# Create the body of the message (HTML version).
		style = '''<style>
			table, th, td {border: 1px solid black;border-collapse: collapse;}
			th, td {padding: 5px;}
			th {text-align: left;}
			</style>'''.strip('\n\t')
		tab = '''<table>
		<tr><th>Table</th><th>RowDiff</th></tr>
		<tr><td>{}</td></tr></table>
		'''.format('</td></tr><tr><td>'.join(['{}</td><td>{}'.format(x,y) for x,y in dd]));
		html = """\
		<html>
			<head>{style}</head>
			<body>
				<h3>New Admin Boundary Data Is Available</h3>
				<p>
				During scheduled admin_bdys importing, differences were detected in the following tables:</br>{tab}<br/>
				Visit the link below to approve/decline submission of the new data to overwrite affected tables</p><br/><br/>
				<a href="{link}">APPROVE/DECLINE</a>
			</body>
		</html>
		""".format(link=c.user_link,tab=tab,style=style)
	
		# Record the MIME type
		content = MIMEText(html, 'html')
		# Attach parts into message container.
		msg.attach(content)
		
		# Send the message.
		conn = smtplib.SMTP(c.user_smtp)
	
		try:
		# sendmail function takes 3 arguments: sender's address, recipient's address, and message to send.
			conn.sendmail(sender, recipients, msg.as_string())
			logger.info('Notifying users {}'.format(recipients))
		finally:	
			conn.quit()
			
	except Exception as exc:
			sys.exit( 'Email sending failed; {0}'.format(exc))		
	
#test values for tablelist t
_T = (('meshblock', ('statsnz_meshblock', 'statsnz_ta', 'meshblock_concordance')), ('nzlocalities', ('nz_locality',)))
	
def oneOrNone(a,options,args):
	'''is A in args OR are none of the options in args'''
	return a in args or not any([True for i in options if i in args]) 
	 
def gather(args,v,c,m):			
	'''fetch data from sources and prepare import schema'''
	logger.info("Beginning meshblock/localities file download")
	t = ()
	#SELECTION['ogr'] = ogrdb.d
	#SELECTION['ogr'] = DatabaseConn_ogr(c)
	s = PExpectSFTP(c)
	v.setup()
	topts = ('meshblock','nzlocalities')
	if oneOrNone('meshblock',topts,args): 
		mbk = Meshblock(c,SELECTION['ogr'],m,s)
		t += (mbk.run(),)
		pass
	if oneOrNone('nzlocalities',topts,args): 
		nzl = NZLocalities(c,SELECTION['ogr'],m,s) 
		t += (nzl.run(),)
		pass
	c.save('t',t)
	logger.info ("Stopping post import for user validation and notifying users")
	if 'detect' in args:
		dd = v.detectdiffs(t)
		if dd: notify(c,dd)

	return t
	
'''
TODO
file name reader, db overwrite
'''
def main():  
	global logger
	
	try:
		opts, args = getopt.getopt(sys.argv[1:], "vh", ["version","help"])
	except getopt.error as msg:
		print (msg+". For help use --help")
		sys.exit(2)
		
	for opt, val in opts:
		if opt in ("-h", "--help"):
			print (__doc__)
			sys.exit(0)
		elif opt in ("-v", "--version"):
			print (__version__)
			sys.exit(0)
					
	#logger = setupLogging()
	if len(args)==0:
		sui = SimpleUI()
		#sui.mainframe.mainloop()
		args = [sui.ret_val,]
		
	if args[0]: process(args)
			
def process(args):
	t = () 
	#_tloc = (('nzlocalities', ('nz_locality',)),)
	
	c = ConfReader()
	m = ColumnMapper(c)
	v = Version(c,m)

	global SELECTION
	with DB(c,'ogr') as ogrdb:
		SELECTION['ogr'] = ogrdb.d
		#if a 't' value is stored we dont want to pre-clean the import schema 
		aopts = [a[1] for a in OPTS]
		if 'reject' in args: 
			v.teardown(c)
			return
		#if "load" requested, import files and recreate+save 'T'
		if oneOrNone('load', aopts,args):
			t = gather(args,v,c,m) 
		else:
			t = c.read('t',flush=False)
		#if "transfer" requested read saved 'T' and transfer to dest
		if oneOrNone('transfer',aopts,args): 
			v.versiontables(t)

	
if __name__ == "__main__":
	main()
	


