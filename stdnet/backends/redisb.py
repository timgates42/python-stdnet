'''Redis backend implementation'''
from copy import copy
import json
from hashlib import sha1
from itertools import chain
from functools import partial
from collections import namedtuple

import stdnet
from stdnet import FieldValueError, CommitException
from stdnet.utils import to_string, map, gen_unique_id, zip,\
                             native_str, flat_mapping
from stdnet.lib import redis

from .base import BackendStructure, query_result, session_result,\
                    instance_session_result, on_result

pairs_to_dict = redis.pairs_to_dict
MIN_FLOAT =-1.e99
EMPTY_DICT = {}


################################################################################
#    prefixes for data
ID = 'id'       # the set of all ids
OBJ = 'obj'     # the hash table for a instance
UNI = 'uni'     # the hashtable for the unique field value to id mapping
IDX = 'idx'     # the set of indexes for a field value
TMP = 'tmp'     # temorary key
################################################################################

def redis_before_send(sender, request, command, **kwargs):
    client  = request.client
    if hasattr(client,'request_info'):
        client.request_info.update({'request':request,
                                    'raw_command':command,
                                    'commands': copy(client.command_stack)})
    
redis.redis_before_send.connect(redis_before_send)


class odmrun(redis.RedisScript):
    script = (redis.read_lua_file('commands.utils'),
              redis.read_lua_file('tabletools'),
              redis.read_lua_file('odm'),
              redis.read_lua_file('odmrun'))
    
    def callback(self, request, response, args, meta=None,
                 iids=None, **options):
        script = args[0]
        if script == 'delete':
            res = (instance_session_result(r,False,r,True,0) for r in response)
            return session_result(meta, res)
        elif script == 'commit':
            res = self._wrap_commit(request, response, iids)
            return session_result(meta, res)
        
    def _wrap_commit(self, request, response, iids):
        for id, iid in zip(response, iids):
            id, flag, info = id
            if int(flag):
                yield instance_session_result(iid, True, id, False, float(info))
            else:
                msg = info.decode(request.encoding)
                yield CommitException(msg)        

#class add_recursive(redis.RedisScript):
#    script = (redis.read_lua_file('commands.utils'),
#              redis.read_lua_file('odm.add_recursive'))
    
    
class load_query(redis.RedisScript):
    '''Rich script for loading a query result into stdnet. It handles
loading of different fields, loading of related fields, sorting and
limiting.'''
    script = (redis.read_lua_file('tabletools'),)
    #          redis.read_lua_file('commands.timeseries'),
    #          redis.read_lua_file('commands.utils'),
    #          redis.read_lua_file('odm.load_query'))
    
    def build(self, response, fields, fields_attributes, encoding):
        fields = tuple(fields) if fields else None
        if fields:
            if len(fields) == 1 and fields[0] == 'id':
                for id in response:
                    yield id,(),{}
            else:
                for id,fdata in response:
                    yield id,fields,dict(zip(fields_attributes,fdata))
        else:
            for id,fdata in response:
                yield id,None,dict(pairs_to_dict(fdata, encoding))
    
    def callback(self, request, response, args, query=None, get=None,
                 fields=None, fields_attributes=None, **kwargs):
        meta = query.meta
        if get:
            tpy = meta.dfields[get].to_python
            return [tpy(v) for v in response]
        else:
            data, related = response
            encoding = request.client.encoding
            data = self.build(data, fields, fields_attributes, encoding)
            related_fields = {}
            if related:
                for fname, rdata, fields in related:
                    fname = native_str(fname, encoding)
                    fields = tuple(native_str(f, encoding) for f in fields)
                    related_fields[fname] =\
                        self.load_related(meta, fname, rdata, fields, encoding)
            return query.backend.objects_from_db(meta, data, related_fields)
        
    def load_related(self, meta, fname, data, fields, encoding):
        '''Parse data for related objects.'''
        field = meta.dfields[fname]
        if field in meta.multifields:
            fmeta = field.structure_class()._meta
            if fmeta.name in ('hashtable','zset'):
                return ((native_str(id, encoding),
                         pairs_to_dict(fdata, encoding)) for \
                        id,fdata in data)
            else:
                return ((native_str(id, encoding),fdata) for id,fdata in data)
        else:
            # this is data for stdmodel instances
            return self.build(data, fields, fields, encoding)
    
    
def structure_session_callback(sm, processed, response):
    if not isinstance(response,Exception):
        results = []
        for p in chain(processed,(response,)):
            if isinstance(p,instance_session_result):
                if p.deleted:
                    if p.persistent:
                        raise InvalidTransaction('Could not delete {0}'\
                                                 .format(p.instance))
                results.append(p)
        if results:
            return session_result(sm.meta, results)
    else:
        return response
        

def results_and_erros(results, result_type):
    if results:
        return [v for v in results if isinstance(v, Exception) or\
                                      isinstance(v, result_type)]
    else:
        return ()
                

def redis_execution(pipe, result_type):
    pipe.request_info = {}
    results = pipe.execute(load_script=True)
    info = pipe.__dict__.pop('request_info', None)
    return info, on_result(results, results_and_erros, result_type)
    
    
################################################################################
##    REDIS QUERY CLASS
################################################################################
class RedisQuery(stdnet.BackendQuery):
    card = None
    script_dep = {'script_dependency': ('build_query','move2set')}
    
    def zism(self, r):
        return r is not None
    
    def sism(self, r):
        return r
    
    def accumulate(self, qs):
        # Accumulate a query
        pipe = self.pipe
        backend = self.backend
        p = 'z' if self.meta.ordering else 's'
        meta = self.meta
        keys = []
        args = []
        # loop over element in queries
        for child in qs:
            if getattr(child, 'backend', None) != backend:
                args.extend(('','' if child is None else child))
            else:
                be = child.backend_query(pipe=pipe)
                keys.append(be.query_key)
                args.extend(('key',be.query_key))
        
        temp_key = True
        if qs.keyword == 'set':
            if qs.name == 'id' and not args:
                key = backend.basekey(meta,'id')
                temp_key = False
            else:
                bk = backend.basekey(meta)
                key = backend.tempkey(meta)
                unique = 'u' if qs.unique else ''
                keys = [bk, key, bk+':*'] + keys
                pipe.script_call('build_query', keys, p, qs.name,
                                 unique, qs.lookup, *args)
        else:
            key = backend.tempkey(meta)
            pipe.script_call('move2set', keys, p,
                             **{'script_dependency':'build_query'})
            if qs.keyword == 'intersect':
                getattr(pipe,p+'interstore')(key, keys, **self.script_dep)
            elif qs.keyword == 'union':
                getattr(pipe,p+'unionstore')(key, keys, **self.script_dep)
            elif qs.keyword == 'diff':
                getattr(pipe,p+'diffstore')(key, keys, **self.script_dep)
            else:
                raise ValueError('Could not perform "{0}" operation'\
                                 .format(qs.keyword))
    
        # If e requires a different field other than id, perform a sort
        # by nosort and get the object field.
        gf = qs._get_field
        if gf and gf != 'id':
            field_attribute = meta.dfields[gf].attname
            bkey = key
            if not temp_key:
                temp_key = True
                key = backend.tempkey(meta)
            okey = backend.basekey(meta, OBJ, '*->' + field_attribute)
            pipe.sort(bkey, by = 'nosort', get = okey, store = key)
            self.card = getattr(pipe,'llen')
            
        if temp_key:
            pipe.expire(key, self.expire)
            
        return 'key',key
        
    def _build(self, pipe=None, **kwargs):
        '''Set up the query for redis'''
        self.pipe = pipe if pipe is not None else self.backend.client.pipeline()
        what, key = self.accumulate(self.queryelem)
        if what == 'key':
            self.query_key = key
        else:
            raise ValueError('Critical error while building query')
    
    def _execute_query(self):
        '''Execute the query without fetching data. Returns the number of
elements in the query.'''
        pipe = self.pipe
        if not self.card:
            if self.meta.ordering:
                self.ismember = getattr(self.backend.client, 'zrank')
                self.card = getattr(self.pipe,'zcard')
                self._check_member = self.zism
            else:
                self.ismember = getattr(self.backend.client, 'sismember')
                self.card = getattr(self.pipe,'scard')
                self._check_member = self.sism
        else:
            self.ismember = None
        
        self.card(self.query_key, script_dependency='build_query')
        pipe.add_callback(lambda processed, result :
                                    query_result(self.query_key, result))
        self.commands, result = redis_execution(pipe, query_result)
        return on_result(result, self._execute_query_result)
    
    def _execute_query_result(self, result):
        self.query_results = result
        return self.query_results[-1].count
    
    def order(self, last):
        '''Perform ordering with respect model fields.'''
        desc = 'DESC' if last.desc else ''
        field = last.name
        nested = last.nested
        nested_args = []
        while nested:
            meta = nested.model._meta
            nested_args.extend((self.backend.basekey(meta),nested.name))
            last = nested
            nested = nested.nested
        meth = ''
        if last.field.internal_type == 'text':
            meth = 'ALPHA'
        
        if field == last.model._meta.pkname():
            field = ''
        args = [field, meth, desc, len(nested_args)//2]
        args.extend(nested_args)
        return args
            
    def _has(self, val):
        r = self.ismember(self.query_key, val)
        return self._check_member(r)
    
    def get_redis_slice(self, slic):
        if slic:
            start = slic.start or 0
            stop = slic.stop
        else:
            start = 0
            stop = None
        return start,stop
    
    def _items(self, slic):
        # Unwind the database query by creating a list of arguments for
        # the load_query lua script
        backend = self.backend
        meta = self.meta
        name = ''
        order = ()
        start, stop = self.get_redis_slice(slic)
        if self.queryelem.ordering:
            order = self.order(self.queryelem.ordering)
        elif meta.ordering:
            name = 'DESC' if meta.ordering.desc else 'ASC'
        elif start or stop is not None:
            order = self.order(meta.get_sorting(meta.pkname()))
        # Wen using the sort algorithm redis requires the number of element
        # not the stop index
        if order:
            name = 'explicit'
            N = self.execute_query()
            if stop is None:
                stop = N
            elif stop < 0:
                stop += N
            if start < 0:
                start += N
            stop -= start
        elif stop is None:
            stop = -1
        get = self.queryelem._get_field or ''
        fields_attributes = None
        keys = (self.query_key, backend.basekey(meta))
        args = [get]
        # if the get_field is available, we simply load that field
        if get:
            if get == 'id':
                fields_attributes = fields = (get,)
            else:
                fields, fields_attributes = meta.backend_fields((get,))
        else:
            fields = self.queryelem.fields or None
            if fields:
                fields = set(fields)
                fields.update(self.queryelem.select_related or ())
                fields = tuple(fields)
            if fields == ('id',):
                fields_attributes = fields
            elif fields:
                fields, fields_attributes = meta.backend_fields(fields)
            else:
                fields_attributes = () 
        args.append(len(fields_attributes))
        args.extend(fields_attributes)
        args.extend(self.related_lua_args())
        args.extend((name,start,stop))
        args.extend(order)
        options = {'fields':fields,
                   'fields_attributes':fields_attributes,
                   'query':self,
                   'get':get}
        return backend.client.script_call('load_query', keys, *args, **options)    

    def related_lua_args(self):
        '''Generator of load_related arguments'''
        related = self.queryelem.select_related
        if not related:
            yield 0
        else:
            meta = self.meta
            yield len(related)
            for rel in related:
                field = meta.dfields[rel]
                typ = field.type if field in meta.multifields else ''
                relmodel = field.relmodel
                bk = self.backend.basekey(relmodel._meta) if relmodel else ''
                fi = related[rel]
                yield bk
                yield field.name
                yield field.attname
                yield typ
                yield len(fi)
                for v in fi:
                    yield v
            

def iteretor_pipelined(f):
    
    def _(self):
        if self.pipelined:
            return iter(())
        else:
            return f(self)
        
    return _

################################################################################
##    STRUCTURES
################################################################################
    
    
class RedisStructure(BackendStructure):
    
    @iteretor_pipelined
    def __iter__(self):
        return self._iter()
        
    def _iter(self):
        raise NotImplementedError()
        
    @property
    def pipelined(self):
        return self.client.pipelined
        
    def delete(self):
        return self.client.delete(self.id)
    
    
class String(RedisStructure):
    
    def flush(self):
        cache = self.instance.cache
        result = None
        data = cache.getvalue()
        if data:
            self.client.append(self.id, data)
            result = True
        return result
    
    def size(self):
        return self.client.strlen(self.id)
    
    def incr(self, num = 1):
        return self.client.incr(self.id, num)
            
    
class Set(RedisStructure):
    
    def flush(self):
        cache = self.instance.cache
        result = None
        if cache.toadd:
            self.client.sadd(self.id, *cache.toadd)
            result = True
        if cache.toremove:
            self.client.srem(self.id, *cache.toremove)
            result = True
        return result
    
    def size(self):
        return self.client.scard(self.id)
    
    def _iter(self):
        return self.client.smembers(self.id)
    

class Zset(RedisStructure):
    '''Redis ordered set structure'''
    def flush(self):
        cache = self.instance.cache
        result = None
        if cache.toadd:
            flat = cache.toadd.flat()
            self.client.zadd(self.id, *flat)
            result = True
        if cache.toremove:
            flat = tuple((el[1] for el in cache.toremove))
            self.client.zrem(self.id, *flat)
            result = True
        return result
    
    def get(self, score):
        r = self.range(score, score, withscores=False)
        if r:
            if len(r) > 1:
                return r
            else:
                return r[0]
    
    def _iter(self):
        return iter(self.irange(withscores=False))
    
    def size(self):
        return self.client.zcard(self.id)
    
    def count(self, start, stop):
        return self.client.zcount(self.id, start, stop)
    
    def range(self, start, end, desc=False, withscores=True, **options):
        return self.async_handle(
                self.client.zrangebyscore(self.id, start, end, desc=desc,
                                          withscores=withscores, **options),
                self._range, withscores)
    
    def irange(self, start=0, stop=-1, desc=False, withscores=True, **options):
        return self.async_handle(
                    self.client.zrange(self.id, start, stop, desc = desc,
                                       withscores = withscores, **options),
                    self._range, withscores)
    
    def ipop_range(self, start, stop=None, withscores=True, **options):
        '''Remove and return a range from the ordered set by rank (index).'''
        return self.async_handle(
                self.client.zpopbyrank(self.id, start, stop,
                                       withscores=withscores, **options),
                self._range, withscores)
        
    def pop_range(self, start, stop=None, withscores=True, **options):
        '''Remove and return a range from the ordered set by score.'''
        return self.async_handle(
                self.client.zpopbyscore(self.id, start, stop,
                                        withscores=withscores, **options),
                self._range, withscores)
    
    def items(self):
        return self.irange()
    
    # PRIVATE
    
    def _range(self, result, withscores):
        if withscores:
            return ((score,v) for v,score in result)
        else:
            return result
        

class List(RedisStructure):
    
    def pop_front(self):
        return self.client.lpop(self.id)
    
    def pop_back(self):
        return self.client.rpop(self.id)

    def block_pop_front(self, timeout):
        return self.client.blpop(self.id, timeout)
    
    def block_pop_front(self, timeout):
        return self.client.brpop(self.id, timeout)
    
    def flush(self):
        cache = self.instance.cache
        result = None
        if cache.front:
            self.client.lpush(self.id, *cache.front)
            result = True
        if cache.back:
            self.client.rpush(self.id, *cache.back)
            result = True
        return result
    
    def size(self):
        return self.client.llen(self.id)
    
    def _iter(self):
        return iter(self.client.lrange(self.id, 0, -1))


class Hash(RedisStructure):
    
    def flush(self):
        cache = self.instance.cache
        result = None
        if cache.toadd:
            self.client.hmset(self.id, cache.toadd)
            result = True
        if cache.toremove:
            self.client.hdel(self.id, *cache.toremove)
            result = True
        return result
        
    def size(self):
        return self.client.hlen(self.id)
    
    def get(self, key):
        return self.client.hget(self.id, key)
    
    def pop(self, key):
        pi = self.pipelined
        p = self.client if pi else self.client.pipeline()
        p.hget(self.id, key).hdel(self.id, key)
        if not pi:
            result = p.execute()
            if result[1]:
                return result[0]
    
    def remove(self, *fields):
        return self.client.hdel(self.id, *fields)
    
    def __contains__(self, key):
        return self.client.hexists(self.id, key)
    
    def _iter(self):
        return iter(self.client.hkeys(self.id))
    
    @iteretor_pipelined
    def values(self):
        return iter(self.client.hvals(self.id))
    
    @iteretor_pipelined        
    def items(self):
        return iter(self.client.hgetall(self.id))
    
    
class TS(RedisStructure):
    '''Redis timeseries implementation is based on the ts.lua script'''
    def flush(self):
        cache = self.instance.cache
        result = None
        if cache.toadd:
            self.client.script_call('ts_commands', self.id, 'add',
                                    *cache.toadd.flat())
            result = True
        if cache.toremove:
            raise NotImplementedError('Cannot remove. TSDEL not implemented')
        return result
    
    def __contains__(self, timestamp):
        return self.client.script_call('ts_commands', self.id, 'exists',
                                       timestamp)
    
    def size(self):
        return self.client.script_call('ts_commands', self.id, 'size')
    
    def count(self, start, stop):
        return self.client.script_call('ts_commands', self.id, 'count',
                                       start, stop)
    
    def times(self, time_start, time_stop, **kwargs):
        return self.client.script_call('ts_commands', self.id, 'times',
                                       time_start, time_stop, **kwargs)
            
    def itimes(self, start=0, stop=-1, **kwargs):
        return self.client.script_call('ts_commands', self.id, 'itimes',
                                       start, stop, **kwargs)
    
    def get(self, dte):
        return self.client.script_call('ts_commands', self.id, 'get', dte)
    
    def rank(self, dte):
        return self.client.script_call('ts_commands', self.id, 'rank', dte)
    
    def pop(self, dte):
        return self.client.script_call('ts_commands', self.id, 'pop', dte)
    
    def ipop(self, index):
        return self.client.script_call('ts_commands', self.id, 'ipop', index)
            
    def range(self, time_start, time_stop, **kwargs):
        return self.client.script_call('ts_commands', self.id, 'range',
                                       time_start, time_stop, **kwargs)
            
    def irange(self, start=0, stop=-1, **kwargs):
        return self.client.script_call('ts_commands', self.id, 'irange',
                                       start, stop, **kwargs)
        
    def pop_range(self, time_start, time_stop, **kwargs):
        return self.client.script_call('ts_commands', self.id, 'pop_range',
                                       time_start, time_stop, **kwargs)
            
    def ipop_range(self, start=0, stop=-1, **kwargs):
        return self.client.script_call('ts_commands', self.id, 'ipop_range',
                                       start, stop, **kwargs)


class NumberArray(RedisStructure):
    
    def flush(self):
        cache = self.instance.cache
        result = None
        if cache.back:
            self.client.script_call('numberarray_pushback', self.id,
                                    *cache.back)
            result = True
        return result
    
    def get(self, index):
        return self.client.script_call('numberarray_getset', self.id,
                                       'get', index+1)
    
    def set(self, value):
        return self.client.script_call('numberarray_getset', self.id,
                                       'set', index+1, value)
    
    def _iter(self):
        return iter(self.client.script_call('numberarray_all_raw', self.id))
    
    def resize(self, size, value = None):
        if value is not None:
            argv = (size,value)
        else:
            argv = (size,)
        return self.client.script_call('numberarray_resize', self.id, *argv)
    
    def size(self):
        return self.client.strlen(self.id)//8
    

class ts_commands(redis.RedisScript):
    script = (redis.read_lua_file('commands.timeseries'),
              redis.read_lua_file('tabletools'),
              redis.read_lua_file('ts'))
    
    
class numberarray_resize(redis.RedisScript):
    script = (redis.read_lua_file('odm.numberarray'),
              '''return array:new(KEYS[1]):resize(unpack(ARGV))''')
    
class numberarray_all_raw(redis.RedisScript):
    script = (redis.read_lua_file('odm.numberarray'),
              '''return array:new(KEYS[1]):all_raw()''')
    
class numberarray_getset(redis.RedisScript):
    script = (redis.read_lua_file('odm.numberarray'),
              '''local a = array:new(KEYS[1])
if ARGV[1] == 'get' then
    return a:get(ARGV[2],true)
else
    a:set(ARGV[2],ARGV[3],true)
end''')
    
class numberarray_pushback(redis.RedisScript):
    script = (redis.read_lua_file('odm.numberarray'),
              '''local a = array:new(KEYS[1])
for _,v in ipairs(ARGV) do
    a:push_back(v,true)
end''')


################################################################################
##    REDIS BACKEND
################################################################################
class BackendDataServer(stdnet.BackendDataServer):
    Query = RedisQuery
    _redis_clients = {}
    struct_map = {'set':Set,
                  'list':List,
                  'zset':Zset,
                  'hashtable':Hash,
                  'ts':TS,
                  'numberarray':NumberArray,
                  'string': String}
        
    def setup_connection(self, address):
        addr = address.split(':')
        if len(addr) == 2:
            try:
                address = (addr[0], int(addr[1]))
            except:
                pass
        rpy = redis.Redis(address=address, **self.params)
        self.execute_command = rpy.execute_command
        self.clear = rpy.flushdb
        #self.keys = rpy.keys
        return rpy
    
    def as_cache(self):
        return self
    
    def set(self, id, value, timeout=None):
        timeout = timeout or 0
        value = self.pickler.dumps(value)
        return self.client.set(id, value, timeout)
    
    def get(self, id, default = None):
        v = self.client.get(id)
        if v:
            return self.pickler.loads(v)
        else:
            return default
        
    def disconnect(self):
        self.client.connection_pool.disconnect()
    
    def set_timeout(self, id, timeout):
        if timeout:
            self.execute_command('EXPIRE', id, timeout)
    
    def has_key(self, id):
        return self.execute_command('EXISTS', id)
    
    def _set(self, id, value, timeout):
        if timeout:
            return self.execute_command('SETEX', id, timeout, value)
        else:
            return self.execute_command('SET', id, value)
    
    def _get(self, id):
        return self.execute_command('GET', id)
            
    def load_scripts(self, *names):
        if not names:
            names = redis.registered_scripts()
        pipe = self.client.pipeline()
        for name in names:
            script = redis.get_script(name)
            if script:
                pipe.script_load(script.script)
        return pipe.execute()
    
    def meta(self, meta):
        '''Extract model metadata for lua script stdnet/lib/lua/odm.lua'''
        pk = meta.pk
        id_type = 3
        id_fields = None
        if pk.type == 'auto':
            id_type = 1
        elif pk.type == 'composite':
            id_type = 2
            id_fields = pk.fields
        return {'namespace': self.basekey(meta),
                'id_name': pk.name,
                'id_type': id_type,
                'id_fields': id_fields,
                'sorted': bool(meta.ordering),
                'multi_fields': [field.name for field in meta.multifields],
                'indices': dict(((idx.attname, idx.unique)\
                                for idx in meta.indices))}
        
    def execute_session(self, session, callback):
        '''Execute a session in redis.'''
        pipe = self.client.pipeline()
        for sm in session:
            meta = sm.meta
            model_type = meta.model._model_type
            if model_type == 'structure':
                self.flush_structure(sm, pipe)
            elif model_type == 'object':
                delquery = sm.get_delete_query(pipe=pipe)
                self.accumulate_delete(pipe, delquery)
                dirty = tuple(sm.iterdirty())
                N = len(dirty)
                if N:
                    lua_data = ['commit', json.dumps(self.meta(meta)), N]
                    processed = []
                    for instance in dirty:
                        state = instance.state()
                        if not instance.is_valid():
                            raise FieldValueError(
                                        json.dumps(instance._dbdata['errors']))
                        score = MIN_FLOAT
                        if meta.ordering:
                            if meta.ordering.auto:
                                score = 'auto %s' % meta.ordering.name.incrby 
                            else:
                                v = getattr(instance,meta.ordering.name,None)
                                if v is not None:
                                    score = meta.ordering.field.scorefun(v)
                        data = instance._dbdata['cleaned_data']
                        if state.persistent:
                            if instance.has_all_data:
                                action = 'override'
                            else:
                                action = 'change'
                            id = state.iid
                        else:
                            action = 'add'
                            id = instance.pkvalue() or ''
                        data = flat_mapping(data)
                        lua_data.extend((action, id, score, len(data)))
                        lua_data.extend(data)
                        processed.append(state.iid)
                    pipe.script_call('odmrun', (), *lua_data,
                                     meta=meta, iids=iids)
        command, result = redis_execution(pipe, session_result)
        return on_result(result, callback, command)
    
    def accumulate_delete(self, pipe, backend_query):
        # Accumulate models queries for a delete. It loops through the
        # related models to build related queries.
        # We pass the pipe since the backend_query may have been evaluated
        # using a different pipe
        if backend_query is None:
            return
        query = backend_query.queryelem
        meta = query.meta
        lua_data = ['delete', json.dumps(self.meta(meta))]
        recursive = []
        rel_managers = []
        for name in meta.related:
            rmanager = getattr(meta.model, name)
            # the related manager model is the same as current model
            if rmanager.model == meta.model:
                pipe.script_call('add_recursive',
                                 (bk, backend_query.query_key, bk + ':*'),
                                 s, rmanager.field.attname)
            else:
                rel_managers.append(rmanager)
        # loop over related managers
        for rmanager in rel_managers:
            rq = rmanager.query_from_query(query).backend_query(pipe=pipe)
            self.accumulate_delete(pipe, rq)
        pipe.script_call('odmrun', (), *lua_data, meta=meta)
        return query
    
    def tempkey(self, meta, name = None):
        return self.basekey(meta, TMP, name if name is not None else\
                                        gen_unique_id())
        
    def flush(self, meta=None, pattern=None):
        '''Flush all model keys from the database'''
        if meta is not None:
            pattern = '{0}*'.format(self.basekey(meta))
        if pattern:
            return self.client.delpattern(pattern)
        
    def clean(self, meta):
        return self.client.delpattern(self.tempkey(meta, '*'))
            
    def model_keys(self, meta):
        pattern = '{0}*'.format(self.basekey(meta))
        return self.client.keys(pattern)            
        
    def instance_keys(self, obj):
        meta = obj._meta
        keys = [self.basekey(meta,OBJ,obj.id)]
        for field in meta.multifields:
            f = getattr(obj,field.attname)
            keys.append(f.id)
        return keys
    
    def flush_structure(self, sm, pipe):
        processed = False
        for instance in chain(sm._delete_query, sm.dirty):
            processed = True
            state = instance.state()
            binstance = instance.backend_structure(pipe)
            n = len(pipe.command_stack)
            binstance.commit()
            script_dependency = []
            for c in pipe.command_stack[n:]:
                script_name = c.options.get('script_name')
                if script_name:
                    script_dependency.append(script_name)
            pipe.exists(binstance.id, script_dependency=script_dependency)
            pipe.add_callback(lambda p,result:\
                    instance_session_result(state.iid,
                                            result,
                                            instance.id,
                                            state.deleted,
                                            0))
        if processed:
            pipe.add_callback(
                        partial(structure_session_callback,sm))
        
    def publish(self, channel, message):
        return self.client.execute_command('PUBLISH', channel, message)
        
    def subscriber(self, **kwargs):
        return redis.Subscriber(self.client, **kwargs)    