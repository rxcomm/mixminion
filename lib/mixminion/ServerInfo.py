# Copyright 2002-2003 Nick Mathewson.  See LICENSE for licensing information.
# $Id: ServerInfo.py,v 1.59 2003/10/19 03:12:02 nickm Exp $

"""mixminion.ServerInfo

   Implementation of server descriptors (as described in the mixminion
   spec).  Includes logic to parse, validate, and generate server
   descriptors.
   """

__all__ = [ 'ServerInfo', 'ServerDirectory' ]

import re
import time

import mixminion.Config
import mixminion.Crypto
import mixminion.MMTPClient
import mixminion.Packet

from mixminion.Common import IntervalSet, LOG, MixError, createPrivateDir, \
    formatBase64, formatDate, formatTime, readPossiblyGzippedFile
from mixminion.Config import ConfigError
from mixminion.Packet import IPV4Info, MMTPHostInfo
from mixminion.Crypto import CryptoError, DIGEST_LEN, pk_check_signature

# Longest allowed Contact email
MAX_CONTACT = 256
# Longest allowed Comments field
MAX_COMMENTS = 1024
# Longest allowed Contact-Fingerprint field
MAX_FINGERPRINT = 128
# Shortest permissible identity key
MIN_IDENTITY_BYTES = 2048 >> 3
# Longest permissible identity key
MAX_IDENTITY_BYTES = 4096 >> 3
# Length of packet key
PACKET_KEY_BYTES = 2048 >> 3
# Length of MMTP key
MMTP_KEY_BYTES = 1024 >> 3

# tmp alias to make this easier to spell.
C = mixminion.Config
class ServerInfo(mixminion.Config._ConfigFile):
    ## Fields: (as in ConfigFile, plus)
    # _isValidated: flag.  Has this serverInfo been fully validated?
    # _validatedDigests: a dict whose keys are already-validated server
    #    digests.  Optional.  Only valid while 'validate' is being called.

    """A ServerInfo object holds a parsed server descriptor."""
    _restrictFormat = 1
    _restrictKeys = _restrictSections = 0
    _syntax = {
        "Server" : { "__SECTION__": ("REQUIRE", None, None),
                     "Descriptor-Version": ("REQUIRE", None, None),
                     "Nickname": ("REQUIRE", C._parseNickname, None),
                     "Identity": ("REQUIRE", C._parsePublicKey, None),
                     "Digest": ("REQUIRE", C._parseBase64, None),
                     "Signature": ("REQUIRE", C._parseBase64, None),
                     "Published": ("REQUIRE", C._parseTime, None),
                     "Valid-After": ("REQUIRE", C._parseDate, None),
                     "Valid-Until": ("REQUIRE", C._parseDate, None),
                     "Contact": ("ALLOW", None, None),
                     "Comments": ("ALLOW", None, None),
                     "Packet-Key": ("REQUIRE", C._parsePublicKey, None),
                     "Contact-Fingerprint": ("ALLOW", None, None),
                     # XXXX010 change these next few to "REQUIRE".
                     "Packet-Formats": ("ALLOW", None, None),#XXXX007 remove
                     "Packet-Versions": ("ALLOW", None, None),
                     "Software": ("ALLOW", None, None),
                     "Secure-Configuration": ("ALLOW", C._parseBoolean, None),
                     "Why-Insecure": ("ALLOW", None, None),
                     },
        "Incoming/MMTP" : {
                     "Version": ("REQUIRE", None, None),
                     "IP": ("ALLOW", C._parseIP, None),#XXXX007 remove
                     "Hostname": ("ALLOW", C._parseHost, None),#XXXX008 require
                     "Port": ("REQUIRE", C._parseInt, None),
                     "Key-Digest": ("ALLOW", C._parseBase64, None),#XXXX007 rmv
                     "Protocols": ("REQUIRE", None, None),
                     "Allow": ("ALLOW*", C._parseAddressSet_allow, None),
                     "Deny": ("ALLOW*", C._parseAddressSet_deny, None),
                     },
        "Outgoing/MMTP" : {
                     "Version": ("REQUIRE", None, None),
                     "Protocols": ("REQUIRE", None, None),
                     "Allow": ("ALLOW*", C._parseAddressSet_allow, None),
                     "Deny": ("ALLOW*", C._parseAddressSet_deny, None),
                     },
        "Delivery/MBOX" : {
                     "Version": ("REQUIRE", None, None),
                     # XXXX006 change to 'REQUIRE'
                     "Maximum-Size": ("ALLOW", C._parseInt, "32"),
                     # XXXX006 change to 'REQUIRE'
                     "Allow-From": ("ALLOW", C._parseBoolean, "yes"),
                     },
        "Delivery/SMTP" : {
                     "Version": ("REQUIRE", None, None),
                     # XXXX006 change to 'REQUIRE'
                     "Maximum-Size": ("ALLOW", C._parseInt, "32"),
                     "Allow-From": ("ALLOW", C._parseBoolean, "yes"),
                     },
        "Delivery/Fragmented" : {
                     "Version": ("REQUIRE", None, None),
                     "Maximum-Fragments": ("REQUIRE", C._parseInt, None),
                     },
        # We never read these values, except to see whether we should
        # regenerate them.  Depending on these options would violate
        # the spec.
        "Testing" : {
                     "Platform": ("ALLOW", None, None),
                     "Configuration": ("ALLOW", None, None),
                     },
        }
    expected_versions = {
         "Server" : ( "Descriptor-Version", "0.2"),
         "Incoming/MMTP" : ("Version", "0.1"),
         "Outgoing/MMTP" : ("Version", "0.1"),
         "Delivery/Fragmented" : ("Version", "0.1"),
         "Delivery/MBOX" : ("Version", "0.1"),
         "Delivery/SMTP" : ("Version", "0.1"),
         }

    def __init__(self, fname=None, string=None, assumeValid=0,
                 validatedDigests=None):
        """Read a server descriptor from a file named <fname>, or from
             <string>.

           If assumeValid is true, don't bother to validate it.

           If the (computed) digest of this descriptor is a key of the dict
              validatedDigests, assume we have already validated it, and
              pass it along.
        """
        self._isValidated = 0
        self._validatedDigests = validatedDigests
        mixminion.Config._ConfigFile.__init__(self, fname, string, assumeValid)
        del self._validatedDigests

    def prevalidate(self, contents):
        for name, ents in contents:
            if name == 'Server':
                for k,v,_ in ents:
                    if k == 'Descriptor-Version' and v.strip() != '0.2':
                        raise ConfigError("Unrecognized descriptor version: %s"
                                          % v.strip())
        
        
        # Remove any sections with unrecognized versions.
        revisedContents = []
        for name, ents in contents:
            v = self.expected_versions.get(name)
            if not v: 
                revisedContents.append((name, ents))
                continue
            versionkey, versionval = v
            for k,v,_ in ents:
                if k == versionkey and v.strip() != versionval:
                    LOG.warn("Skipping %s section with unrecognized version %s"
                             , name, v.strip())
                    break
            else:
                revisedContents.append((name, ents))

        return revisedContents

    def validate(self, lines, contents):
        ####
        # Check 'Server' section.
        server = self['Server']
        if server['Descriptor-Version'] != '0.2':
            raise ConfigError("Unrecognized descriptor version %r",
                              server['Descriptor-Version'])

        ####
        # Check the digest of file
        digest = getServerInfoDigest(contents)
        if digest != server['Digest']:
            raise ConfigError("Invalid digest")

        # Have we already validated this particular ServerInfo?
        if (self._validatedDigests and
            self._validatedDigests.has_key(digest)):
            self._isValidated = 1
            return

        # Validate the rest of the server section.
        identityKey = server['Identity']
        identityBytes = identityKey.get_modulus_bytes()
        if not (MIN_IDENTITY_BYTES <= identityBytes <= MAX_IDENTITY_BYTES):
            raise ConfigError("Invalid length on identity key")
        if server['Published'] > time.time() + 600:
            raise ConfigError("Server published in the future")
        if server['Valid-Until'] <= server['Valid-After']:
            raise ConfigError("Server is never valid")
        if server['Contact'] and len(server['Contact']) > MAX_CONTACT:
            raise ConfigError("Contact too long")
        if server['Comments'] and len(server['Comments']) > MAX_COMMENTS:
            raise ConfigError("Comments too long")
        if server['Contact-Fingerprint'] and \
               len(server['Contact-Fingerprint']) > MAX_FINGERPRINT:
            raise ConfigError("Contact-Fingerprint too long")

        packetKeyBytes = server['Packet-Key'].get_modulus_bytes()
        if packetKeyBytes != PACKET_KEY_BYTES:
            raise ConfigError("Invalid length on packet key")

        ####
        # Check signature
        try:
            signedDigest = pk_check_signature(server['Signature'], identityKey)
        except CryptoError:
            raise ConfigError("Invalid signature")

        if digest != signedDigest:
            raise ConfigError("Signed digest is incorrect")

        ## Incoming/MMTP section
        inMMTP = self['Incoming/MMTP']
        if inMMTP:
            if inMMTP['Version'] != '0.1':
                raise ConfigError("Unrecognized MMTP descriptor version %s"%
                                  inMMTP['Version'])
            if len(inMMTP['Key-Digest']) != DIGEST_LEN:
                raise ConfigError("Invalid key digest %s"%
                                  formatBase64(inMMTP['Key-Digest']))
            if not inMMTP['IP'] and not inMMTP['Hostname']:
                raise ConfigError("Incoming/MMTP section has neither IP nor hostname")

        ## Outgoing/MMTP section
        outMMTP = self['Outgoing/MMTP']
        if outMMTP:
            if outMMTP['Version'] != '0.1':
                raise ConfigError("Unrecognized MMTP descriptor version %s"%
                                  inMMTP['Version'])

        # FFFF When a better client module system exists, check the
        # FFFF module descriptors.

        self._isValidated = 1

    def getNickname(self):
        """Returns this server's nickname"""
        return self['Server']['Nickname']

    def getDigest(self):
        """Returns the declared (not computed) digest of this server
           descriptor."""
        return self['Server']['Digest']

    def getIP(self):
        """Returns this server's IP address"""
        return self['Incoming/MMTP'].get('IP')

    def getHostname(self):
        """DOCDOC"""
        return self['Incoming/MMTP'].get("Hostname")

    def getPort(self):
        """Returns this server's IP port"""
        return self['Incoming/MMTP']['Port']

    def getPacketKey(self):
        """Returns the RSA key this server uses to decrypt messages"""
        return self['Server']['Packet-Key']

    def getKeyDigest(self):
        """Returns a hash of this server's MMTP key"""
        return mixminion.Crypto.sha1(
            mixminion.Crypto.pk_encode_public_key(self['Server']['Identity']))
        #return self['Incoming/MMTP']['Key-Digest']

    def getIPV4Info(self):
        """Returns a mixminion.Packet.IPV4Info object for routing messages
           to this server."""
        return IPV4Info(self.getIP(), self.getPort(), self.getKeyDigest())

    def getMMTPHostInfo(self):
        """DOCDOC"""
        return MMTPHostInfo(get.getHostname(), self.getPort(), self.getKeyDigest())
    
    def getRoutingInfo(self):
        return self.getIPV4Info()

    def getIdentity(self):
        return self['Server']['Identity']

    def getIncomingMMTPProtocols(self):
        inc = self['Incoming/MMTP']
        if not inc.get("Version"):
            return []
        return [ s.strip() for s in inc["Protocols"].split(",") ]

    def getOutgoingMMTPProtocols(self):
        inc = self['Outgoing/MMTP']
        if not inc.get("Version"):
            return []
        return [ s.strip() for s in inc["Protocols"].split(",") ]

    def canRelayTo(self, otherDesc):
        """DOCDOC"""
        if self.hasSameNicknameAs(otherDesc):
            return 1
        myOutProtocols = self.getOutgoingMMTPProtocols()
        otherInProtocols = otherDesc.getIncomingMMTPProtocols()
        for out in myOutProtocols:
            if out in otherInProtocols:
                return 1
        return 0

    def canStartAt(self):
        """DOCDOC"""
        myInProtocols = self.getIncomingMMTPProtocols()
        for out in mixminion.MMTPClient.BlockingClientConnection.PROTOCOL_VERSIONS:
            if out in myInProtocols:
                return 1
        return 0

    def getRoutingFor(self, otherDesc, swap=0):
        """DOCDOC"""
        #XXXX006 use this!
        assert self.canRelayTo(otherDesc)
        assert 0 <= swap <= 1
        if self.getHostname() and otherDesc.getHostname():
            ri = otherDesc.getMMTPHostInfo().pack()
            rt = [mixminion.Packet.FWD_HOST_TYPE,
                  mixminion.Packet.SWAP_FWD_HOST_TYPE][swap]
        else:
            ri = otherDesc.getIPV4Info().pack()
            rt = [mixminion.Packet.FWD_IPV4_TYPE,
                  mixminion.Packet.SWAP_FWD_IPV4_TYPE][swap]

        return rt, ri
        
    def getCaps(self):
        # FFFF refactor this once we have client addresses.
        caps = []
        if not self['Incoming/MMTP'].get('Version'):
            return caps
        if self['Delivery/MBOX'].get('Version'):
            caps.append('mbox')
        if self['Delivery/SMTP'].get('Version'):
            caps.append('smtp')
        # XXXX This next check is highly bogus.
        if self['Outgoing/MMTP'].get('Version'):
            caps.append('relay')
        if self['Delivery/Fragmented'].get('Version'):
            caps.append('frag')
        return caps

    def isSameDescriptorAs(self, other):
        """DOCDOC"""
        return self.getDigest() == other.getDigest()

    def hasSameNicknameAs(self, other):
        """DOCDOC"""
        return self.getNickname().lower() == other.getNickname().lower()

    def isValidated(self):
        """Return true iff this ServerInfo has been validated"""
        return self._isValidated

    def getIntervalSet(self):
        """Return an IntervalSet covering all the time at which this
           ServerInfo is valid."""
        return IntervalSet([(self['Server']['Valid-After'],
                             self['Server']['Valid-Until'])])

    def isExpiredAt(self, when):
        """Return true iff this ServerInfo expires before time 'when'."""
        return self['Server']['Valid-Until'] < when

    def isValidAt(self, when):
        """Return true iff this ServerInfo is valid at time 'when'."""
        return (self['Server']['Valid-After'] <= when <=
                self['Server']['Valid-Until'])

    def isValidFrom(self, startAt, endAt):
        """Return true iff this ServerInfo is valid at all time from 'startAt'
           to 'endAt'."""
        assert startAt < endAt
        return (self['Server']['Valid-After'] <= startAt and
                endAt <= self['Server']['Valid-Until'])

    def isValidAtPartOf(self, startAt, endAt):
        """Return true iff this ServerInfo is valid at some time between
           'startAt' and 'endAt'."""
        assert startAt < endAt
        va = self['Server']['Valid-After']
        vu = self['Server']['Valid-Until']
        return ((startAt <= va and va <= endAt) or
                (startAt <= vu and vu <= endAt) or
                (va <= startAt and endAt <= vu))

    def isNewerThan(self, other):
        """Return true iff this ServerInfo was published after 'other',
           where 'other' is either a time or a ServerInfo."""
        if isinstance(other, ServerInfo):
            other = other['Server']['Published']
        return self['Server']['Published'] > other

    def isSupersededBy(self, others):
        """Return true iff this ServerInfo is superseded by the other
           ServerInfos in 'others'.

           A ServerInfo is superseded when, for all time it is valid,
           a more-recently-published descriptor with the same nickname
           is also valid.

           This function is only accurate when called with two valid
           server descriptors.
        """
        valid = self.getIntervalSet()
        for o in others:
            if (o.isNewerThan(self) and
                o.getNickname().lower() == self.getNickname().lower()):
                valid -= o.getIntervalSet()
        return valid.isEmpty()

#----------------------------------------------------------------------
# Server Directories

# Regex used to split a big directory along '[Server]' lines.
_server_header_re = re.compile(r'^\[\s*Server\s*\]\s*\n', re.M)
class ServerDirectory:
    """Minimal client-side implementation of directory parsing.  This will
       become very inefficient when directories get big, but we won't have
       that problem for a while."""
    ##Fields:
    # allServers: list of validated ServerInfo objects, in no particular order.
    # servers: sub-list of self.allServers, containing all of the servers
    #    that are recommended.
    # goodServerNames: list of lowercased nicknames for the recommended
    #    servers in this directory.
    # header: a _DirectoryHeader object for the non-serverinfo part of this
    #    directory.
    def __init__(self, string=None, fname=None, validatedDigests=None):
        """Create a new ServerDirectory object, either from a literal <string>
           (if specified) or a filename [possibly gzipped].

           If validatedDigests is provided, it must be a dict whose keys
           are the digests of already-validated descriptors.  Any descriptor
           whose (calculated) digest matches doesn't need to be validated
           again.
        """
        if string:
            contents = string
        else:
            contents = readPossiblyGzippedFile(fname)

        contents = _cleanForDigest(contents)

        # First, get the digest.  Then we can break everything up.
        digest = _getDirectoryDigestImpl(contents)

        # This isn't a good way to do this, but what the hey.
        sections = _server_header_re.split(contents)
        del contents
        headercontents = sections[0]
        servercontents = [ "[Server]\n%s"%s for s in sections[1:] ]

        self.header = _DirectoryHeader(headercontents, digest)
        self.goodServerNames = [name.strip().lower() for name in
                   self.header['Directory']['Recommended-Servers'].split(",")]
        servers = [ ServerInfo(string=s,
                               validatedDigests=validatedDigests)
                    for s in servercontents ]
        self.allServers = servers[:]
        self.servers = [ s for s in servers
                         if s.getNickname().lower() in self.goodServerNames ]

    def getServers(self):
        """Return a list of recommended ServerInfo objects in this directory"""
        return self.servers

    def getAllServers(self):
        """Return a list of all (even unrecommended) ServerInfo objects in
           this directory."""
        return self.allServers

    def getRecommendedNicknames(self):
        """Return a list of the (lowercased) nicknames of all of the
           recommended servers in this directory."""
        return self.goodServerNames

    def __getitem__(self, item):
        return self.header[item]

    def get(self, item, default=None):
        return self.header.get(item, default)

class _DirectoryHeader(mixminion.Config._ConfigFile):
    """Internal object: used to parse, validate, and store fields in a
       directory's header sections.
    """
    ## Fields:
    # expectedDigest: the 20-byte digest we expect to find in this
    #    directory's header.
    _restrictFormat = 1
    _restrictKeys = _restrictSections = 0
    _syntax = {
        'Directory': { "__SECTION__": ("REQUIRE", None, None),
                       "Version": ("REQUIRE", None, None),
                       "Published": ("REQUIRE", C._parseTime, None),
                       "Valid-After": ("REQUIRE", C._parseDate, None),
                       "Valid-Until": ("REQUIRE", C._parseDate, None),
                       "Recommended-Servers": ("REQUIRE", None, None),
                       },
        'Signature': {"__SECTION__": ("REQUIRE", None, None),
                 "DirectoryIdentity": ("REQUIRE", C._parsePublicKey, None),
                 "DirectoryDigest": ("REQUIRE", C._parseBase64, None),
                 "DirectorySignature": ("REQUIRE", C._parseBase64, None),
                      },
        'Recommended-Software': {"__SECTION__": ("ALLOW", None, None),
                "MixminionClient": ("ALLOW", None, None),
                "MixminionServer": ("ALLOW", None, None), }
        }
    def __init__(self, contents, expectedDigest):
        """Parse a directory header out of a provided string; validate it
           given the digest we expect to find for the file.
        """
        self.expectedDigest = expectedDigest
        mixminion.Config._ConfigFile.__init__(self, string=contents)

    def prevalidate(self, contents):
        for name, ents in contents:
            if name == 'Directory':
                for k,v,_ in ents:
                    if k == 'Version' and v.strip() != '0.2':
                        raise ConfigError("Unrecognized directory version")

        return contents

    def validate(self, lines, contents):
        direc = self['Directory']
        if direc['Version'] != "0.2":
            raise ConfigError("Unrecognized directory version")
        if direc['Published'] > time.time() + 600:
            raise ConfigError("Directory published in the future")
        if direc['Valid-Until'] <= direc['Valid-After']:
            raise ConfigError("Directory is never valid")

        sig = self['Signature']
        identityKey = sig['DirectoryIdentity']
        identityBytes = identityKey.get_modulus_bytes()
        if not (MIN_IDENTITY_BYTES <= identityBytes <= MAX_IDENTITY_BYTES):
            raise ConfigError("Invalid length on identity key")

        # Now, at last, we check the digest
        if self.expectedDigest != sig['DirectoryDigest']:
            raise ConfigError("Invalid digest")

        try:
            signedDigest = pk_check_signature(sig['DirectorySignature'],
                                              identityKey)
        except CryptoError:
            raise ConfigError("Invalid signature")
        if self.expectedDigest != signedDigest:
            raise ConfigError("Signed digest was incorrect")

#----------------------------------------------------------------------
def getServerInfoDigest(info):
    """Calculate the digest of a server descriptor"""
    return _getServerInfoDigestImpl(info, None)

def signServerInfo(info, rsa):
    """Sign a server descriptor.  <info> should be a well-formed server
       descriptor, with Digest: and Signature: lines present but with
       no values."""
    return _getServerInfoDigestImpl(info, rsa)

_leading_whitespace_re = re.compile(r'^[ \t]+', re.M)
_trailing_whitespace_re = re.compile(r'[ \t]+$', re.M)
_abnormal_line_ending_re = re.compile(r'\r\n?')
def _cleanForDigest(s):
    """Helper function: clean line endings and whitespace so we can calculate
       our digests with uniform results."""
    # should be shared with config, serverinfo.
    s = _abnormal_line_ending_re.sub("\n", s)
    s = _trailing_whitespace_re.sub("", s)
    s = _leading_whitespace_re.sub("", s)
    if not s.endswith("\n"):
        s += "\n"
    return s

def _getDigestImpl(info, regex, digestField=None, sigField=None, rsa=None):
    """Helper method.  Calculates the correct digest of a server descriptor
       or directory
       (as provided in a string).  If rsa is provided, signs the digest and
       creates a new descriptor.  Otherwise just returns the digest.

       info -- the string to digest or sign.
       regex -- a compiled regex that matches the line containing the digest
          and the line containing the signature.
       digestField -- If not signing, None.  Otherwise, the name of the digest
          field.
       sigField -- If not signing, None.  Otherwise, the name of the signature
          field.
       rsa -- our public key
       """
    info = _cleanForDigest(info)
    def replaceFn(m):
        s = m.group(0)
        return s[:s.index(':')+1]
    info = regex.sub(replaceFn, info, 2)
    digest = mixminion.Crypto.sha1(info)

    if rsa is None:
        return digest

    signature = mixminion.Crypto.pk_sign(digest,rsa)
    digest = formatBase64(digest)
    signature = formatBase64(signature)
    def replaceFn2(s, digest=digest, signature=signature,
                   digestField=digestField, sigField=sigField):
        if s.group(0).startswith(digestField):
            return "%s: %s" % (digestField, digest)
        else:
            assert s.group(0).startswith(sigField)
            return "%s: %s" % (sigField, signature)

    info = regex.sub(replaceFn2, info, 2)
    return info

_special_line_re = re.compile(r'^(?:Digest|Signature):.*$', re.M)
def _getServerInfoDigestImpl(info, rsa=None):
    return _getDigestImpl(info, _special_line_re, "Digest", "Signature", rsa)

_dir_special_line_re = re.compile(r'^Directory(?:Digest|Signature):.*$', re.M)
def _getDirectoryDigestImpl(directory, rsa=None):
    return _getDigestImpl(directory, _dir_special_line_re,
                          "DirectoryDigest", "DirectorySignature", rsa)
