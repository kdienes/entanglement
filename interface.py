#!/usr/bin/python3
# Copyright (C) 2017, Hadron Industries, Inc.
# Entanglement is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License version 3
# as published by the Free Software Foundation. It is distributed
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the file
# LICENSE for details.


import asyncio, logging, ssl
import protocol
from util import CertHash

logger = logging.getLogger("hadron.entanglement")

class SynchronizableMeta(type):
    '''A metaclass for capturing Synchronizable classes.  In python3.6, no metaclass will be needed; __init__subclass will be sufficient.'''

    def __init__(cls, name, bases, _dict):
        if cls.sync_registry:
            if not isinstance(cls.sync_registry, SyncRegistry):
                raise TypeError("Class {cls} sets sync_registry to something that is not a SyncRegistry".format(cls = cls.__name__))
            cls.sync_registry.register_syncable(cls.sync_type, cls)

    sync_registry = property(doc = "A registry of classes that this Syncable belongs to.  Registries can be associated with a connection; only classes in registries associated with a connection are permitted to be synchronized over that connection")

    @sync_registry.getter
    def sync_registry(inst): return inst.__dict__.get('sync_registry', None)
    
class Synchronizable( metaclass = SynchronizableMeta):

    def to_sync(self):
        '''Return a dictionary containing the attributes of self that should be synchronized.'''
        raise NotImplementedError

    @classmethod
    def sync_receive(self, msg):
        raise NotImplementedError()

    @classmethod
    def sync_should_listen(self, msg):
        '''Return True or raise SynchronizationUnauthorized'''
        return True
    
    def __hash__(self):
        '''Hash all the primary keys.'''
        return sum(map(lambda x: getattr(self, x).__hash__(), self.__class__.sync_primary_keys))

    def __eq__(self, other):
        '''Return true if the primary keys of self match the primary keys of other'''
        return (self.__class__ == other.__class__) and \
            ball(map(lambda k: getattr(self,k).__eq__(getattr(other,k)), self.__class__.sync_primary_keys))

    sync_primary_keys = property(doc = "tuple of attributes comprising  primary keys")
    @sync_primary_keys.getter
    def sync_primary_keys(self):
        raise NotImplementedError
    
    class _Sync_type:
        "The type of object being synchronized"

        def __get__(self, instance, owner):
            return owner.__name__

    sync_type = _Sync_type()
    del _Sync_type
    
class SyncRegistry:
    '''A registry of Syncable classes.  A connection may accept
    synchronization from one or more registries.  A Syncable typically
    belongs to one registry.  A registry can be thought of as a schema of
    related objects implementing some related synchronizable interface.'''

    def __init__(self):
        self.registry = {}

    def register_syncable(self, type_name, cls):
        if type_name in self.registry:
            raise ValueError("`{} is already registered in this registry.".format(type_name))
        self.registry[type_name] = cls

class SyncError(RuntimeError): pass

class WrongSyncDestination(SyncError):

    def __init__(msg = None, *args, dest = None, got_hash = None,
                 **kwargs):
        if not msg and dest:
            msg = "Incorrect certificate hash received from connection to {dest}".format(dest)
            if got_hash: msg = msg + " (got {got})".format(got = got_hash)
        super().__init__(msg, *args, **kwargs)
        
class SyncManager:

    '''A SyncManager manages connections to other Synchronization
    endpoints.  A SyncManager presents a single identity to the rest
    of the world represented by a private key and certificate.
    SyncManager includes the logic necessary to act as a client;
    SyncServer extends SyncManager with logic necessary to accept
    connections.
    '''

    def __init__(self, cert, port, *, key = None, loop = None,
                 capath = None, cafile = None,
                 registries = []):
        if loop:
            self.loop = loop
            self.loop_allocated = False
        else:
            self.loop = asyncio.new_event_loop()
            self.loop_allocated = True
        self._transports = []
        self._destinations = {}
        self._connections = {}
        self._connecting = {}
        self._ssl = self._new_ssl(cert, key = key,
                                 capath = capath, cafile = cafile)
        self.registries = registries
        self.port = port

    def _new_ssl(self, cert, key, capath, cafile):
        sslctx = ssl.create_default_context()
        sslctx.load_cert_chain(cert, key)
        sslctx.load_verify_locations(cafile=cafile, capath = capath)
        return sslctx

    def _protocol_factory_client(self, dest):
        "This is more of a factory factory than a factory.  Construct a protocol object for a connection to a given outgoing SyncDestination"
        return lambda: protocol.SyncProtocol(manager = self, dest = dest)

    def _protocol_factory_server(self):
        "Factory factory for server connections"
        return lambda: protocol.SyncProtocol(manager = self, incoming = True)
    

    async     def _create_connection(self, dest):
        "Create a connection on the loop.  This is effectively a coroutine."
        loop = self.loop
        delta = 1
        close_transport = None #Close this transport if we fail to
        #connect There are two levels of try; the outer catches exceptions
        #that end all connection attempts and cleans up the cache of
        #destinations we're connecting to.
        try:
            while True: # not connected
                if dest.connect_at > loop.time():
                    logger.info("Waiting until {time} to connect to {dest}".format(
                    time = time.ctime(dest.connect_at),
                    dest = dest))
                    delta = dest.connect_at-loop.time()
                    await asyncio.sleep(delta)
                    delta = min(2*delta, 10*60)
                try:
                    logger.debug("Connecting to {hash} at {host}".format(
                        hash = dest.cert_hash,
                        host = dest.host))
                    transport, protocol = await \
                                          loop.create_connection(self._protocol_factory_client(dest),
                                                                 port = self.port, ssl = self._ssl,
                                                                 host = dest.host,
                                                                 server_hostname = dest.server_hostname)
                    logger.debug("Transport connection to {dest} made".format(dest = dest))
                    close_transport = transport
                    if protocol.cert_hash != dest.cert_hash:
                        raise WrongSyncDestination(dest = dest, got_hash = protocol.cert_hash)
                
                    await dest.connected(self, protocol)
                    self._connections[dest.cert_hash] = protocol
                    close_transport = None
                    logger.info("Connected to {hash} at {host}".format(
                        hash = dest.cert_hash,
                        host = dest.host))
                    return transport, protocol
                except asyncio.futures.CancelledError:
                    logger.debug("Connection to {dest} canceled".format(dest = dest))
                    raise
                except (SyntaxError, Typeerror, LookupError, ValueError, WrongSyncDestination) as e:
                    logger.exception("Connection to {} failed".format(dest.cert_hash))
                    raise
                except:
                    logger.exception("Error connecting to  {}".format(dest))
                    dest.connect_at = loop.time() + delta
        finally:
            del self._connecting[dest.cert_hash]
            if close_transport: close_transport.close()

    
                

    def add_destination(self, dest):
        if dest.cert_hash in self._destinations:
            raise KeyError("{} is already a destination".format(repr(dest)))
        self._destinations[dest.cert_hash] = dest
        assert dest.protocol is None
        assert dest.cert_hash not in self._connecting
        self._connecting[dest.cert_hash] = self.loop.create_task(self._create_connection(dest))
        return self._connecting[dest.cert_hash]
    

    def run_until_complete(self, *args):
        return self.loop.run_until_complete(*args)

    def _sync_receive(self, msg):
        self._validate_message(msg)
        cls = self._find_registered_class(msg['_sync_type'])
        if self.should_listen(msg, cls) is not True:
            # Failure should raise because ignoring an exception takes
            # active work, leading to a small probability of errors.
            # However, active authorization should be an explicit true
            # not falling off the end of a function.
            raise SyntaxError("should_listen must either return True or raise")
        if msg['_sync_authorized'] != self:
            raise SyntaxError("When SyncManager.should_listen is overwridden, you must call super().should_listen")
        del msg['_sync_authorized']
        cls.sync_receive(msg)

    def _validate_message(self, msg):
        if not isinstance(msg, dict):
            raise protocol.MessageError('Message is a {} not a dict'.format(msg.__class__.__name__))
        for k in msg:
            if k.startswith('_') and k not in protocol.SYNc_magic_attributes:
                raise protocol.MessageError('{} is not a valid attribute in a sync message'.format(k))

    def should_listen(self, msg, cls):
        msg['_sync_authorized'] = self #To confirm we've been called.
        if cls.sync_registry.should_listen(msg, cls)is not True:
            raise SyntaxError('should_listen must return True or raise')
        if cls.sync_should_listen(msg) is not True:
            raise SyntaxError('sync_should_listen must return True or raise')
        return True
    
    def _find_registered_class(self, name):
        for reg in self.registries:
            if name in reg.registry: return reg.registry[name]
        raise UnregisteredSyncClass('{} is not registered for this manager'.format(name))
    
    def close(self):
        if not hasattr(self,'_transports'): return
        for t in self._transports:
            if t(): t().close()
        if self.loop_allocated:
            self.loop.call_soon(self.loop.stop)
            self.loop.run_forever()
            #Two trips through the loop because of ssl
            self.loop.call_soon(self.loop.stop)
            self.loop.run_forever()
            self.loop.close()
        del self._transports
        del self.loop

    def __del__(self):
        self.close()
        

class SyncServer(SyncManager):

    "A SyncManager that accepts incoming connections"

    def __init__(self, cert, port, *, host, cafile = None, capath = None,
                 key = None, **kwargs):
        super().__init__(cert, port, capath = capath,
                         cafile = cafile, key = key,
                         **kwargs)
        self.host = host
        self._server = None
        self._ssl_server = self._new_ssl(cert, key = key, cafile = cafile,
                                         capath = capath)
        self._ssl_server.check_hostname = False
        self._server = self.loop.run_until_complete(self.loop.create_server(
            self._protocol_factory_server(),
#            host = host,
            port = port,
            ssl = self._ssl_server,
            reuse_address = True, reuse_port = True))

    def close(self):
        if hasattr(self, '_server') and self._server:
            self._server.close()
            self._server = None
        super().close()

        

class SyncDestination:

    '''A SyncDestination represents a SyncManager other than ourselves that can receive (and generate) synchronizations.  The Synchronizable and subclasses of SyncDestination must cooperate to make sure that receiving and object does not create a loop by trying to Synchronize that object back to the sender.  One solution is for should_send on SyncDestination to return False (or raise) if the outgoing object is received from this destination.'''

    def __init__(self, cert_hash, name, *,
                 host = None, bw_per_sec = None,
                 server_hostname = None):
        if server_hostname is None: server_hostname = host
        self.host = host
        self.cert_hash = CertHash(cert_hash)
        self.name = name
        self.server_hostname = server_hostname
        self.bw_per_sec = bw_per_sec
        self.protocol = None
        self.connect_at = 0
        
    def __repr__(self):
        return "<SyncDestination {{name: '{name}', hash: {hash}}}".format(
            name = self.name,
            hash = self.cert_hash)

    def should_send(self, obj, manager , **kwargs):
        return True

    def should_listen(self, msg, manager, cls, **kwargs):
        '''Must return True or raise'''
        return True

    

    async def connected(self, manager, protocol):
        '''Interface point; called by manager when an outgoing or incoming
        connection is made to the destination.  Except in the case of
        an unknown destination connecting to a server, the destination
        is known and already in the manager's list of connecting
        destinations.  The destination will not be added to the
        manager's list of connections until this coroutine returns
        true.  However, incoming synchronizations will be processed
        and will result in calls to the destination's should_listen
        method.  If this raises, the connection will be closed and aborted.
        '''
        self.protocol = protocol
        return
    
