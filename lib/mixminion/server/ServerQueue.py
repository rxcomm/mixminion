# Copyright 2002-2003 Nick Mathewson.  See LICENSE for licensing information.
# $Id: ServerQueue.py,v 1.18 2003/05/31 12:52:55 nickm Exp $

"""mixminion.server.ServerQueue

   Facility for fairly secure, directory-based, unordered queues.
   """

import os
import time
import stat
import sys
import cPickle
import threading
import types

from mixminion.Common import MixError, MixFatalError, secureDelete, LOG, \
     createPrivateDir, readPickled, writePickled, formatTime
from mixminion.Crypto import getCommonPRNG

__all__ = [ 'Queue', 'DeliveryQueue', 'TimedMixPool', 'CottrellMixPool',
            'BinomialCottrellMixPool' ]

# Mode to pass to open(2) for creating a new file, and dying if it already
# exists.
_NEW_MESSAGE_FLAGS = os.O_WRONLY+os.O_CREAT+os.O_EXCL
# On windows or mac, binary != text.
_NEW_MESSAGE_FLAGS += getattr(os, 'O_BINARY', 0)

# Any inp_* files older than INPUT_TIMEOUT seconds old are assumed to be
# trash.
INPUT_TIMEOUT = 6000

class Queue:
    """A Queue is an unordered collection of files with secure insert, move,
       and delete operations.

       Implementation: a queue is a directory of 'messages'.  Each
       filename in the directory has a name in one of the following
       formats:
             rmv_HANDLE  (A message waiting to be deleted)
             msg_HANDLE  (A message waiting in the queue.
             inp_HANDLE  (An incomplete message being created.)

       (Where HANDLE is a randomly chosen 8-character string of characters
       chosen from 'A-Za-z0-9+-'.  [Collision probability is negligable, and
       collisions are detected.])

       Threading notes:  Although Queue itself is threadsafe, you'll want
       to synchronize around any multistep operations that you want to
       run atomicly.  Use Queue.lock() and Queue.unlock() for this.

       In the Mixminion server, no queue currently has more than one producer
       or more than one consumer ... so synchronization turns out to be
       fairly easy.
       """
       # Threading: If we ever get more than one producer, we're fine.  With
       #    more than one consumer, we'll need to modify DeliveryQueue below.

    # Fields:   dir--the location of the queue.
    #           n_entries: the number of complete messages in the queue.
    #                 <0 if we haven't counted yet.
    #           _lock: A lock that must be held while modifying or accessing
    #                 the queue object.  Filesystem operations are allowed
    #                 without holding the lock, but they must not be visible
    #                 to users of the queue.
    def __init__(self, location, create=0, scrub=0):
        """Creates a queue object for a given directory, 'location'.  If
           'create' is true, creates the directory if necessary.  If 'scrub'
           is true, removes any incomplete or invalidated messages from the
           Queue."""

        secureDelete([]) # Make sure secureDelete is configured. HACK!

        self._lock = threading.RLock()
        self.dir = location

        if not os.path.isabs(location):
            LOG.warn("Queue path %s isn't absolute.", location)

        if os.path.exists(location) and not os.path.isdir(location):
            raise MixFatalError("%s is not a directory" % location)

        createPrivateDir(location, nocreate=(not create))

        if scrub:
            self.cleanQueue()

        # Count messages on first time through.
        self.n_entries = -1

    def lock(self):
        """Prevent access to this queue from other threads."""
        self._lock.acquire()

    def unlock(self):
        """Release the lock on this queue."""
        self._lock.release()

    def queueMessage(self, contents):
        """Creates a new message in the queue whose contents are 'contents',
           and returns a handle to that message."""
        f, handle = self.openNewMessage()
        f.write(contents)
        self.finishMessage(f, handle) # handles locking
        return handle

    def queueObject(self, object):
        """Queue an object using cPickle, and return a handle to that
           object."""
        f, handle = self.openNewMessage()
        cPickle.dump(object, f, 1)
        self.finishMessage(f, handle) # handles locking
        return handle

    def count(self, recount=0):
        """Returns the number of complete messages in the queue."""
        try:
            self._lock.acquire()
            if self.n_entries >= 0 and not recount:
                return self.n_entries
            else:
                res = 0
                for fn in os.listdir(self.dir):
                    if fn.startswith("msg_"):
                        res += 1
                self.n_entries = res
                return res
        finally:
            self._lock.release()

    def pickRandom(self, count=None):
        """Returns a list of 'count' handles to messages in this queue.
           The messages are chosen randomly, and returned in a random order.

           If there are fewer than 'count' messages in the queue, all the
           messages will be retained."""
        handles = self.getAllMessages() # handles locking

        return getCommonPRNG().shuffle(handles, count)

    def getAllMessages(self):
        """Returns handles for all messages currently in the queue.
           Note: this ordering is not guaranteed to be random"""
        self._lock.acquire()
        hs = [fn[4:] for fn in os.listdir(self.dir) if fn.startswith("msg_")]
        self._lock.release()
        return hs

    def removeMessage(self, handle):
        """Given a handle, removes the corresponding message from the queue."""
        self.__changeState(handle, "msg", "rmv") # handles locking.

    def removeAll(self, secureDeleteFn=None):
        """Removes all messages from this queue."""
        try:
            self._lock.acquire()
            for m in os.listdir(self.dir):
                if m[:4] in ('inp_', 'msg_'):
                    self.__changeState(m[4:], m[:3], "rmv")
            self.n_entries = 0
            self.cleanQueue(secureDeleteFn)
        finally:
            self._lock.release()

    def moveMessage(self, handle, queue):
        """Given a handle and a queue, moves the corresponding message from
           this queue to the queue provided.  Returns a new handle for
           the message in the destination queue."""
        # Since we're switching handles, we don't want to just rename;
        # We really want to copy and delete the old file.
        try:
            self._lock.acquire()
            newHandle = queue.queueMessage(self.messageContents(handle))
            self.removeMessage(handle)
        finally:
            self._lock.release()

        return newHandle

    def getMessagePath(self, handle):
        """Given a handle for an existing message, return the name of the
           file that contains that message."""
        # We don't need to lock here: the handle is still valid, or it isn't.
        return os.path.join(self.dir, "msg_"+handle)

    def openMessage(self, handle):
        """Given a handle for an existing message, returns a file descriptor
           open to read that message."""
        # We don't need to lock here; the handle is still valid, or it isn't.
        return open(os.path.join(self.dir, "msg_"+handle), 'rb')

    def messageContents(self, handle):
        """Given a message handle, returns the contents of the corresponding
           message."""
        try:
            self._lock.acquire()
            f = open(os.path.join(self.dir, "msg_"+handle), 'rb')
            s = f.read()
            f.close()
            return s
        finally:
            self._lock.release()

    def getObject(self, handle):
        """Given a message handle, read and unpickle the contents of the
           corresponding message."""
        try:
            self._lock.acquire()
            f = open(os.path.join(self.dir, "msg_"+handle), 'rb')
            res = cPickle.load(f)
            f.close()
            return res
        finally:
            self._lock.release()

    def openNewMessage(self):
        """Returns (file, handle) tuple to create a new message.  Once
           you're done writing, you must call finishMessage to
           commit your changes, or abortMessage to reject them."""
        file, handle = getCommonPRNG().openNewFile(self.dir, "inp_", 1)
        return file, handle

    def finishMessage(self, f, handle):
        """Given a file and a corresponding handle, closes the file
           commits the corresponding message."""
        f.close()
        self.__changeState(handle, "inp", "msg")

    def abortMessage(self, f, handle):
        """Given a file and a corresponding handle, closes the file
           rejects the corresponding message."""
        f.close()
        self.__changeState(handle, "inp", "rmv")

    def cleanQueue(self, secureDeleteFn=None):
        """Removes all timed-out or trash messages from the queue.

           If secureDeleteFn is provided, it is called with a list of
           filenames to be removed.  Otherwise, files are removed using
           secureDelete.

           Returns 1 if a clean is already in progress; otherwise
           returns 0.
        """
        # We don't need to hold the lock here; we synchronize via the
        # filesystem.

        rmv = []
        allowedTime = int(time.time()) - INPUT_TIMEOUT
        for m in os.listdir(self.dir):
            if m.startswith("rmv_"):
                rmv.append(os.path.join(self.dir, m))
            elif m.startswith("inp_"):
                try:
                    s = os.stat(m)
                    if s[stat.ST_MTIME] < allowedTime:
                        self.__changeState(m[4:], "inp", "rmv")
                        rmv.append(os.path.join(self.dir, m))
                except OSError:
                    pass
        if secureDeleteFn:
            secureDeleteFn(rmv)
        else:
            secureDelete(rmv, blocking=1)
        return 0

    def __changeState(self, handle, s1, s2):
        """Helper method: changes the state of message 'handle' from 's1'
           to 's2', and changes the internal count."""
        try:
            self._lock.acquire()
            try:
                os.rename(os.path.join(self.dir, s1+"_"+handle),
                          os.path.join(self.dir, s2+"_"+handle))
            except OSError, e:
                contents = os.listdir(self.dir)
                LOG.error("Error while trying to change %s from %s to %s: %s",
                          handle, s1, s2, e)
                LOG.error("Directory %s contains: %s", self.dir, contents)
                self.count(1)
                return

            if self.n_entries < 0:
                return
            if s1 == 'msg' and s2 != 'msg':
                self.n_entries -= 1
            elif s1 != 'msg' and s2 == 'msg':
                self.n_entries += 1
        finally:
            self._lock.release()


class _DeliveryState:
    """Helper class: holds the state needed to schedule delivery or
       eventual abandonmont of a message in a DeliveryQueue."""
    ## Fields:
    # queuedTime: time at which the corresponding message was first
    #    inserted into the queue.
    # lastAttempt: The most recent time at which we attempted to
    #    deliver the message. (None means 'never').
    def __init__(self, queuedTime=None, lastAttempt=None):
        """Create a new _DeliveryState for a message received at
           queuedTime (default now), whose last delivery attempt was
           at lastAttempt (default never)."""
        if queuedTime is None:
            queuedTime = time.time()
        self.queuedTime = queuedTime
        self.lastAttempt = lastAttempt

    def __getstate__(self):
        # For pickling.  All future versions of deliverystate will pickle
        #   to a tuple, whose first element will be a version string.
        return ("V0", self.queuedTime, self.lastAttempt)

    def __setstate__(self, state):
        # For pickling.
        if state[0] == "V0":
            self.queuedTime = state[1]
            self.lastAttempt = state[2]
        else:
            raise MixFatalError("Unrecognized delivery state")

    def getNextAttempt(self, retrySchedule, now=None):
        """Return the next time when we should try to deliver this message
           according to the provided retrySchedule.  If the time returned
           is in the past, then immediate delivery is okay.  If the time
           returned is None, this message has expired and should be forgotten.
        """
        if not now:
            now = time.time()

        last = self.lastAttempt

        # If we've never tried to deliver the message, it's ready to
        # go immediately.
        if last is None:
            return now

        # Otherwise, we count from the time the message was first queued,
        # until we find a scheduled delivery that falls after the last
        # attempted delivery.
        #
        # This scheduled delivery may be in the past.  That's okay: it only
        # means that we've missed a scheduled delivery, and we can try again
        # immediately.
        attempt = self.queuedTime
        for interval in retrySchedule:
            attempt += interval
            if attempt > last:
                return attempt

        # Oops: there are no scheduled deliveries after the last delivery.
        # Time to drop this message.
        return None

    def setLastAttempt(self, when):
        """Update time of the last attempted delivery."""
        self.lastAttempt = when

class DeliveryQueue(Queue):
    """A DeliveryQueue implements a queue that greedily sends messages to
       outgoing streams that occasionally fail.  All underlying messages
       are pickled objects.  Additionally, we store metadata about
       attempted deliveries in the past, so we know when to schedule the
       next delivery.

       This class is abstract. Implementors of this class should subclass
       it to add a _deliverMessages method.  Multiple invocations of this
       method may be active at a given time.  Upon success or failure, this
       method should cause deliverySucceeded or deliveryFailed to be called
       as appropriate.

       Users of this class will probably only want to call the
       queueMessage, sendReadyMessages, and nextMessageReadyAt methods.

       This class caches information about the directory state; it won't
       play nice if multiple instances are looking at the same directory.
    """
    ###
    # Fields:
    #    sendable -- A list of handles for all messages that we're not
    #           currently sending.
    #    pending -- Dict from handle->time_sent, for all messages that we're
    #           currently sending.
    #    retrySchedule -- a list of intervals at which delivery of messages
    #           should be reattempted, as described in "setRetrySchedule".
    #    deliveryState -- a dict from handle->_DeliveryState object for
    #           all handles.
    #    nextAttempt -- a dict from handle->time-of-next-scheduled-delivery,
    #           for all handles.  Not meaningful for handles in 'pending'.
    #           If the time is in the past, delivery can be tried now.
    #           If None, the message may be removable.
    #
    # XXXX Refactor as many of these fields as possible into _DeliveryState.
    #
    # Files:
    #    meta_* : a pickled _DeliveryState object for each message in the
    #        queue.
    #    rmv_meta_*: a dead metafile, waiting for removal.

    def __init__(self, location, retrySchedule=None, now=None):
        """Create a new DeliveryQueue object that stores its files in
           <location>.  If retrySchedule is provided, it is interpreted as
           in setRetrySchedule."""
        Queue.__init__(self, location, create=1, scrub=1)
        self.retrySchedule = None
        self._rescan()
        if retrySchedule is not None:
            self.setRetrySchedule(retrySchedule, now)
        else:
            self.setRetrySchedule([0], now)
        self._repOk()

    def setRetrySchedule(self, schedule, now=None):
        """Set the retry schedule for this queue.  A retry schedule is
           a list of integers, each representing a number of seconds.
           For example, a schedule of [ 120, 120, 3600, 3600 ] will
           cause undeliverable messages to be retried after 2 minutes,
           then 2 minutes later, then 1 hour later, then 1 hour later.

           Retry schedules are not strictly guaranteed, for two reasons:
             1) Message delivery can fail _unretriably_, in which case
                no further attempts are made.
             2) Retries are only actually attempted when sendReadyMessages
                is called.  If the schedule specifies retry attempts at
                10-second intervals, but sendReadyMessages is invoked only
                every 30 minutes, messages will only me retried once every
                30 minutes.
        """
        try:
            self._lock.acquire()
            self.retrySchedule = schedule[:]
            self._rebuildNextAttempt(now)
        finally:
            self._lock.release()

    def _rescan(self, now=None):
        """Helper: Rebuild the internal state of this queue from the
           underlying directory.  Trashes 'pending' and 'sendable'."""
        try:
            self._lock.acquire()
            self.pending = {}
            self.nextAttempt = {}
            self.sendable = self.getAllMessages()
            self._loadState()
            self._rebuildNextAttempt(now)
            self._repOk()
        finally:
            self._lock.release()

    def _loadState(self):
        """Read all DeliveryState objects from the disk."""
        # must hold lock.
        self.deliveryState = {}
        for h in self.getAllMessages():
            fn = os.path.join(self.dir, "meta_"+h)
            if os.path.exists(fn):
                self.deliveryState[h] = readPickled(fn)
            else:
                LOG.warn("No metadata for file handle %s", h)
                obj = self.getObject(h)
                #XXXX005 remove this.
                if isinstance(obj, types.TupleType) and len(obj) == 4:
                    # This message is in an obsolete format from 0.0.3.
                    # We'd repair it, but packets from 0.0.3 are incompatible
                    # anyway.
                    LOG.info("Removing item %s in obsolete format", h)
                    self.removeMessage(h)
                    continue
                
                self.deliveryState[h] = _DeliveryState()
                self._writeState(h)

        for fn in os.listdir(self.dir):
            if fn.startswith("meta_"):
                h = fn[5:]
                if not self.deliveryState.has_key(h):
                    LOG.warn("Metadata for nonexistant handle %s", h)
                os.unlink(os.path.join(self.dir, fn))

    def _writeState(self, h):
        """Helper method: writes out the metadata for handle 'h'.  If that
           handle has been removed, removes the metadata.
        """
        fn = os.path.join(self.dir, "meta_"+h)
        ds = self.deliveryState.get(h)
        if ds is not None:
            writePickled(fn, self.deliveryState[h])
        else:
            try:
                os.rename(fn, os.path.join(self.dir, "rmv_meta_"+h))
            except OSError:
                pass

    def _rebuildNextAttempt(self, now=None):
        """Helper: Reconstruct self.nextAttempt from self.retrySchedule and
           self.deliveryState.

           Callers must hold self._lock.
        """
        if self.retrySchedule is None:
            rs = [0]
        else:
            rs = self.retrySchedule

        nextAttempt = {}
        for h, ds in self.deliveryState.items():
            nextAttempt[h] = ds.getNextAttempt(rs, now)
        self.nextAttempt = nextAttempt
        self._repOk()

    def _repOk(self):
        """Raise an assertion error if the internal state of this object is
           nonsensical."""
        #XXXX004 Later in the release cycle, we should call this less.  It
        #XXXX004 adds ~8-9ms on my laptop for ~400 messages
        try:
            self._lock.acquire()

            allHandles = self.getAllMessages()
            knownHandles = self.pending.keys() + self.sendable
            allHandles.sort()
            knownHandles.sort()
            if allHandles != knownHandles:
                LOG.error("_repOK: %s != %s", allHandles, knownHandles)
                assert allHandles == knownHandles
            dsHandles = self.deliveryState.keys()
            naHandles = self.nextAttempt.keys()
            dsHandles.sort()
            naHandles.sort()
            assert allHandles == dsHandles
            assert allHandles == naHandles
        finally:
            self._lock.release()

    def queueMessage(self, msg):
        if 1: raise MixError("Tried to call DeliveryQueue.queueMessage.")

    def queueDeliveryMessage(self, msg, now=None):
        """Schedule a message for delivery.
             msg -- the message.  This can be any pickleable object.
        """
        assert self.retrySchedule is not None
        try:
            self._lock.acquire()
            handle = self.queueObject(msg)
            self.sendable.append(handle)
            ds = self.deliveryState[handle] = _DeliveryState(now)
            self.nextAttempt[handle] = \
                     ds.getNextAttempt(self.retrySchedule, now)
            LOG.trace("ServerQueue got message %s for %s",
                      handle, self.dir)
            self._writeState(handle)
        finally:
            self._lock.release()

        self._repOk()
        return handle

    def _inspect(self,handle):
        """Returns a (msg, inserted, lastAttempt, nextAttempt) tuple
           for a given message handle.  For testing. """
        self._repOk()
        o = self.getObject(handle)
        return (o,
                self.deliveryState[handle].queuedTime,
                self.deliveryState[handle].lastAttempt,
                self.nextAttempt[handle])

    def removeExpiredMessages(self, now=None):
        """Remove every message expired in this queue according to the
           current schedule.  Ordinarily, messages are removed when
           their last delivery is over.  Occasionally, however,
           changing the schedule while the system is down can make calling
           this method useful."""
        try:
            self._lock.acquire()
            for h in self.sendable:
                if self.nextAttempt[h] is None:
                    self.removeMessage(h)
        finally:
            self._lock.release()

    def sendReadyMessages(self, now=None):
        """Sends all messages which are not already being sent, and which
           are scheduled to be sent."""
        assert self.retrySchedule is not None
        if now is None:
            now = time.time()
        LOG.trace("ServerQueue checking for deliverable messages in %s",
                  self.dir)
        try:
            self._lock.acquire()
            handles = self.sendable
            messages = []
            self.sendable = []
            for h in self.pending.keys():
                LOG.trace("     [%s] is pending delivery", h)
            for h in handles:
                assert not self.pending.has_key(h)
                next = self.nextAttempt[h]
                if next is None:
                    LOG.trace("     [%s] is expired", h)
                    self.removeMessage(h)
                elif next <= now:
                    LOG.trace("     [%s] is ready for delivery", h)
                    messages.append( (h, self.getObject(h)) )
                    self.pending[h] = now
                else:
                    LOG.trace("     [%s] is not yet ready for redelivery", h)
                    self.sendable.append(h)
        finally:
            self._lock.release()
        self._repOk()
        if messages:
            self._deliverMessages(messages)
            self._repOk()

    def _deliverMessages(self, msgList):
        """Abstract method; Invoked with a list of (handle, message)
           tuples every time we have a batch of messages to send.

           For every handle in the list, delierySucceeded or deliveryFailed
           should eventually be called, or the message will sit in the queue
           indefinitely, without being retried."""

        # We could implement this as a single _deliverMessage(h,addr)
        # method, but that wouldn't allow implementations to batch
        # messages being sent to the same address.

        raise NotImplementedError("_deliverMessages")

    def removeMessage(self, handle):
        try:
            self._lock.acquire()
            Queue.removeMessage(self, handle)
            try:
                del self.pending[handle]
                pending = 1
            except KeyError:
                pending = 0

            try:
                del self.deliveryState[handle]
            except KeyError:
                LOG.error("Removing message %s with no delivery state", handle)
            try:
                del self.nextAttempt[handle]
            except KeyError:
                LOG.error("Removing message %s with no nextAttempt", handle)

            try:
                del self.sendable[self.sendable.index(handle)]
            except ValueError:
                if not pending:
                    LOG.error("Removing message %s in neither "
                              "'sendable' nor 'pending' list", handle)

            self._writeState(handle)
        finally:
            self._lock.release()

    def removeAll(self, secureDeleteFn=None):
        try:
            self._lock.acquire()
            for m in os.listdir(self.dir):
                if m[:5] == 'meta_':
                    os.rename(os.path.join(self.dir, m),
                              os.path.join(self.dir, "rmv_"+m))
            Queue.removeAll(self, secureDeleteFn)
            self.deliveryState = {}
            self.pending = {}
            self.nextAttempt = {}
            self.sendable = []
            self.cleanQueue()
        finally:
            self._lock.release()


    def deliverySucceeded(self, handle):
        """Removes a message from the outgoing queue.  This method
           should be invoked after the corresponding message has been
           successfully delivered.
        """
        assert self.retrySchedule is not None

        LOG.trace("ServerQueue got successful delivery for %s from %s",
                  handle, self.dir)
        self.removeMessage(handle)

    def deliveryFailed(self, handle, retriable=0, now=None):
        """Removes a message from the outgoing queue, or requeues it
           for delivery at a later time.  This method should be
           invoked after the corresponding message has been
           unsuccessfully delivered."""
        assert self.retrySchedule is not None
        LOG.trace("ServerQueue failed to deliver %s from %s",
                  handle, self.dir)
        try:
            self._lock.acquire()
            try:
                lastAttempt = self.pending[handle]
            except KeyError:
                # This should never happen
                LOG.error_exc(sys.exc_info(),
                              "Handle %s was not pending", handle)
                return

            if retriable:
                # If we can retry the message, update the deliveryState
                # with the most recent attempt, and see if there's another
                # attempt in the future.
                try:
                    ds = self.deliveryState[handle]
                except KeyError:
                    # This should never happen
                    LOG.error_exc(sys.exc_info(),
                                  "Handle %s had no state", handle)
                    ds = self.deliveryState[handle] = _DeliveryState(now)

                ds.setLastAttempt(lastAttempt)
                nextAttempt = ds.getNextAttempt(self.retrySchedule, now)
                if nextAttempt is not None:
                    LOG.trace("     (We'll try %s again at %s)", handle,
                              formatTime(nextAttempt, 1))
                    # There is another scheduled delivery attempt.  Remember
                    # it, mark the message sendable again, and save our state.
                    self.nextAttempt[handle] = nextAttempt
                    self.sendable.append(handle)
                    try:
                        del self.pending[handle]
                    except KeyError:
                        LOG.error("Handle %s was not pending", handle)

                    self._repOk()
                    self._writeState(handle)
                    return

                # Otherwise, fallthrough.

            # If we reach this point, the message is undeliverable.
            LOG.trace("     (Giving up on %s)", handle)
            self.removeMessage(handle)
            self._repOk()
        finally:
            self._lock.release()

class TimedMixPool(Queue):
    """A TimedMixPool holds a group of files, and returns some of them
       as requested, according to a mixing algorithm that sends a batch
       of messages every N seconds."""
    ## Fields:
    #   interval: scanning interval, in seconds.
    def __init__(self, location, interval=600):
        """Create a TimedMixPool that sends its entire batch of messages
           every 'interval' seconds."""
        Queue.__init__(self, location, create=1, scrub=1)
        self.interval = interval

    def getBatch(self):
        """Return handles for all messages that the pool is currently ready
           to send in the next batch"""
        return self.pickRandom()

    def getInterval(self):
        return self.interval

class CottrellMixPool(TimedMixPool):
    """A CottrellMixPool holds a group of files, and returns some of them
       as requested, according the Cottrell (timed dynamic-pool) mixing
       algorithm from Mixmaster."""
    ## Fields:
    # interval: scanning interval, in seconds.
    # minPool: Minimum number of messages to keep in pool.
    # minSend: Minimum number of messages above minPool before we consider
    #      sending.
    # sendRate: Largest fraction of the pool to send at a time.
    def __init__(self, location, interval=600, minPool=6, minSend=1,
                 sendRate=.7):
        """Create a new queue that yields a batch of message every 'interval'
           seconds, always keeps <minPool> messages in the pool, never sends
           unless it has <minPool>+<minSend> messages, and never sends more
           than <sendRate> * the corrent pool size.

           If 'minSend'==1, this is a real Cottrell (type II style) mix pool.
           Otherwise, this is a generic 'timed dynamic-pool' mix pool.  (Note
           that there is still a matter of some controversy whether it ever
           makes sense to set minSend != 1.)
           """
        # Note that there was a bit of confusion here: earlier versions
        # implemented an algorithm called "mixmaster" that wasn't actually the
        # mixmaster algorithm.  I picked up the other algorithm from an early
        # draft of Roger, Paul, and Andrei's 'Batching Taxonomy' paper (since
        # corrected); they seem to have gotten it from Anja Jerichow's
        # Phd. thesis ("Generalisation and Security Improvement of
        # Mix-mediated Anonymous Communication") of 2000.
        #
        # *THIS* is the algorithm that the current 'Batching Taxonomy' paper
        # says that Cottrell says is the real thing.

        TimedMixPool.__init__(self, location, interval)
        self.minPool = minPool
        self.minSend = minSend
        self.sendRate = sendRate

    def _getBatchSize(self):
        "Helper method: returns the number of messages to send."
        pool = self.count()
        if pool >= (self.minPool + self.minSend):
            sendable = pool - self.minPool
            return min(sendable, max(1, int(pool * self.sendRate)))
        else:
            return 0

    def getBatch(self):
        "Returns a list of handles for the next batch of messages to send."
        n = self._getBatchSize()
        if n:
            return self.pickRandom(n)
        else:
            return []

class _BinomialMixin:
    """Mixin class.  Given a MixPool that defines a _getBatchSize function,
       replaces the getBatch function with one that -- instead of sending N
       messages from a pool of size P, sends each message with probability
       N/P."""
    def getBatch(self):
        n = self._getBatchSize()
        count = self.count()
        if n == 0 or count == 0:
            return []
        msgProbability = n / float(count)
        rng = getCommonPRNG()
        return rng.shuffle([ h for h in self.getAllMessages()
                             if rng.getFloat() < msgProbability ])

class BinomialCottrellMixPool(_BinomialMixin,CottrellMixPool):
    """Same algorithm as CottrellMixPool, but instead of sending N messages
       from the pool of size P, sends each message with probability N/P."""
