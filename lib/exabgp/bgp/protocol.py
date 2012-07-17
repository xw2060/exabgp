# encoding: utf-8
"""
protocol.py

Created by Thomas Mangin on 2009-08-25.
Copyright (c) 2009-2012 Exa Networks. All rights reserved.
"""

#import os
#import copy
import time
from struct import unpack,error

from exabgp.rib.table import Table
from exabgp.rib.delta import Delta

from exabgp.structure.utils import hexa
from exabgp.protocol.family import AFI,SAFI
from exabgp.protocol.ip.inet import mask_to_bytes
from exabgp.bgp.route import RouteBGP
from exabgp.bgp.message.open.asn import ASN,AS_TRANS
from exabgp.bgp.connection import Connection
from exabgp.bgp.message import Message,defix,Failure
from exabgp.bgp.message.nop import NOP
from exabgp.bgp.message.open import Open
from exabgp.bgp.message.open.holdtime import HoldTime
from exabgp.bgp.message.open.routerid import RouterID
from exabgp.bgp.message.open.capability import Unknown,Parameter,Capabilities
from exabgp.bgp.message.open.capability.id import CapabilityID
from exabgp.bgp.message.open.capability.mp import MultiProtocol
from exabgp.bgp.message.open.capability.refresh import RouteRefresh,CiscoRouteRefresh
from exabgp.bgp.message.open.capability.graceful import Graceful
from exabgp.bgp.message.open.capability.ms import MultiSession
from exabgp.bgp.message.open.capability.addpath import AddPath
from exabgp.bgp.message.update import Update
from exabgp.bgp.message.update.eor import EOR
from exabgp.bgp.message.keepalive import KeepAlive
from exabgp.bgp.message.notification import Notification, Notify #, NotConnected
from exabgp.bgp.message.update.nlri import NLRI,PathInfo,Labels,RouteDistinguisher
from exabgp.bgp.message.update.attribute import AttributeID
from exabgp.bgp.message.update.attribute.attributes import Attributes
from exabgp.bgp.message.update.attribute.flag import Flag
from exabgp.bgp.message.update.attribute.origin import Origin
from exabgp.bgp.message.update.attribute.nexthop import NextHop
from exabgp.bgp.message.update.attribute.med import MED
from exabgp.bgp.message.update.attribute.localpref import LocalPreference
from exabgp.bgp.message.update.attribute.aggregator import Aggregator
from exabgp.bgp.message.update.attribute.communities import Community,Communities,ECommunity,ECommunities
from exabgp.bgp.message.update.attribute.aspath import ASPath,AS4Path
from exabgp.bgp.message.update.attribute.originatorid import OriginatorID
from exabgp.bgp.message.update.attribute.clusterlist import ClusterList

from exabgp.structure.processes import ProcessError

from exabgp.structure.log import Logger,LazyFormat
logger = Logger()

# This is the number of chuncked message we are willing to buffer, not the number of routes
MAX_BACKLOG = 15000

# Generate an NLRI from a BGP packet receive
def BGPNLRI (afi,safi,bgp,has_multiple_path):
	labels = []
	rd = ''

	if has_multiple_path:
		path_identifier = bgp[:4]
		bgp = bgp[4:]
	else:
		path_identifier = ''

	mask = ord(bgp[0])
	bgp = bgp[1:]

	if SAFI(safi).has_label():
		while bgp and mask >= 8:
			label = int(unpack('!L',chr(0) + bgp[:3])[0])
			bgp = bgp[3:]
			labels.append(label>>4)
			mask -= 24 # 3 bytes
			if label & 1:
				break
			# This is a route withdrawal, or next-hop
			if label == 0x000000 or label == 0x80000:
				break

	if SAFI(safi).has_rd():
		mask -= 8*8 # the 8 bytes of the route distinguisher
		rd = bgp[:8]
		bgp = bgp[8:]

	if mask < 0:
		raise Notify(3,0,'invalid length in NLRI prefix')

	if not bgp and mask:
		raise Notify(3,0,'not enough data for the mask provided to decode the NLRI')

	size = mask_to_bytes[mask]

	if len(bgp) < size:
		raise Notify(3,0,'could not decode route with AFI %d sand SAFI %d' % (afi,safi))

	network = bgp[:size]
	# XXX: The padding calculation should really go into the NLRI class
	padding = '\0'*(NLRI.length[afi]-size)
	prefix = network + padding
	nlri = NLRI(afi,safi,prefix,mask)

	# XXX: Not the best interface but will do for now
	if safi:
		nlri.safi = SAFI(safi)

	if path_identifier:
		nlri.path_info = PathInfo(packed=path_identifier)
	if labels:
		nlri.labels = Labels(labels)
	if rd:
		nlri.rd = RouteDistinguisher(rd)
	return nlri


# README: Move all the old packet decoding in another file to clean up the includes here, as it is not used anyway

class Protocol (object):
	decode = True
	strict = False

	def __init__ (self,peer,connection=None):
		self.peer = peer
		self.neighbor = peer.neighbor
		self.connection = connection
		# for which afi/safi pair should we encode path information (addpath)
		self.use_path = None

		self._delta = Delta(Table(peer))
		self._asn4 = False
		self._messages = []
		self._frozen = 0
		self.message_size = 4096

		# The holdtime / families negocicated between the two peers
		self.hold_time = None
		self.families = []

	# XXX: we use self.peer.neighbor.peer_address when we could use self.neighbor.peer_address

	def me (self,message):
		return "Peer %15s ASN %-7s %s" % (self.peer.neighbor.peer_address,self.peer.neighbor.peer_as,message)

	def connect (self):
		# allows to test the protocol code using modified StringIO with a extra 'pending' function
		if not self.connection:
			peer = self.neighbor.peer_address
			local = self.neighbor.local_address
			md5 = self.neighbor.md5
			ttl = self.neighbor.ttl
			self.connection = Connection(peer,local,md5,ttl)

			if self.peer.neighbor.peer_updates:
				message = 'neighbor %s connected\n' % self.peer.neighbor.peer_address
				try:
					proc = self.peer.supervisor.processes
					for name in proc.notify(self.neighbor.peer_address):
						proc.write(name,message)
				except ProcessError:
					raise Failure('Could not send message(s) to helper program(s) : %s' % message)

	def check_keepalive (self):
		left = int (self.connection.last_read  + self.hold_time - time.time())
		if left <= 0:
			raise Notify(4,0)
		return left

	def close (self,reason='unspecified'):
		#self._delta.last = 0
		if self.connection:
			# must be first otherwise we could have a loop caused by the raise in the below
			self.connection.close()
			self.connection = None

			if self.peer.neighbor.peer_updates:
				message = 'neighbor %s down - %s\n' % (self.peer.neighbor.peer_address,reason)
				try:
					proc = self.peer.supervisor.processes
					for name in proc.notify(self.neighbor.peer_address):
						proc.write(name,message)
				except ProcessError:
					raise Failure('Could not send message(s) to helper program(s) : %s' % message)

	# Read from network .......................................................

	def read_message (self):
		# This call reset the time for the timeout in
		if not self.connection.pending(True):
			return NOP('')

		length = 19
		data = ''
		while length:
			if self.connection.pending():
				delta = self.connection.read(length)
				data += delta
				length -= len(delta)
				# The socket is closed
				if not data:
					raise Failure('The TCP connection is closed')

		if data[:16] != Message.MARKER:
			# We are speaking BGP - send us a valid Marker
			raise Notify(1,1,'The packet received does not contain a BGP marker')

		raw_length = data[16:18]
		length = unpack('!H',raw_length)[0]
		msg = data[18]

		if ( length < 19 or length > 4096):
			# BAD Message Length
			raise Notify(1,2)

		if (
			(msg == Open.TYPE and length < 29) or
			(msg == Update.TYPE and length < 23) or
			(msg == Notification.TYPE and length < 21) or
			(msg == KeepAlive.TYPE and length != 19)
		):
			# MUST send the faulty length back
			raise Notify(1,2,raw_length)
			#(msg == RouteRefresh.TYPE and length != 23)

		length -= 19
		data = ''
		while length:
			if self.connection.pending():
				delta = self.connection.read(length)
				data += delta
				length -= len(delta)
				# The socket is closed
				if not data:
					raise Failure('The TCP connection is closed')

		if msg == Notification.TYPE:
			raise Notification(ord(data[0]),ord(data[1]))

		if msg == KeepAlive.TYPE:
			return self.KeepAliveFactory(data)

		if msg == Open.TYPE:
			return self.OpenFactory(data)

		if msg == Update.TYPE:
			if self.neighbor.parse_routes:
				update = self.UpdateFactory(data)
				return update
			else:
				return NOP('')

		if self.strict:
			raise Notify(1,3,msg)

		return NOP(data)

	def read_open (self,_open,ip):
		message = self.read_message()

		if message.TYPE == NOP.TYPE:
			return message

		if message.TYPE != Open.TYPE:
			raise Notify(5,1,'The first packet recevied is not an open message (%s)' % message)

		if _open.asn.asn4() and not message.capabilities.announced(CapabilityID.FOUR_BYTES_ASN):
			raise Notify(2,0,'We have an ASN4 and you do not speak it. bye.')

		self._asn4 = message.capabilities.announced(CapabilityID.FOUR_BYTES_ASN)

		if message.asn == AS_TRANS:
			peer_as = message.capabilities[CapabilityID.FOUR_BYTES_ASN]
		else:
			peer_as = message.asn

		if peer_as != self.neighbor.peer_as:
			raise Notify(2,2,'ASN in OPEN (%d) did not match ASN expected (%d)' % (message.asn,self.neighbor.peer_as))

		# RFC 6286 : http://tools.ietf.org/html/rfc6286
		#if message.router_id == RouterID('0.0.0.0'):
		#	message.router_id = RouterID(ip)
		if message.router_id == RouterID('0.0.0.0'):
			raise Notify(2,3,'0.0.0.0 is an invalid router_id according to RFC6286')
		if message.router_id == self.neighbor.router_id and message.asn == self.neighbor.local_as:
			raise Notify(2,3,'BGP Indendifier collision (%s) on IBGP according to RFC 6286' % message.router_id)

		if message.hold_time < 3:
			raise Notify(2,6,'Hold Time is invalid (%d)' % message.hold_time)
		if message.hold_time >= 3:
			self.hold_time = HoldTime(min(self.neighbor.hold_time,message.hold_time))

		if message.capabilities.announced(CapabilityID.MULTIPROTOCOL_EXTENSIONS) \
		and _open.capabilities.announced(CapabilityID.MULTIPROTOCOL_EXTENSIONS):
			for family in message.capabilities[CapabilityID.MULTIPROTOCOL_EXTENSIONS]:
				if family in _open.capabilities[CapabilityID.MULTIPROTOCOL_EXTENSIONS]:
					self.families.append(family)

		# XXX: Does not work as the capa is not yet defined
		if message.capabilities.announced(CapabilityID.EXTENDED_MESSAGE):
			# untested !
			if self.peer.bgp.message_size:
				self.message_size = self.peer.bgp.message_size

# README: This limit what we are announcing may cause some issue if you add new family and SIGHUP
# README: So it is commented until I make my mind to add it or not (as Juniper complain about mismatch capabilities)
#		# Those are the capacity we need to announce those routes
#		for family in _open.capabilities[CapabilityID.MULTIPROTOCOL_EXTENSIONS]:
#			# if the peer does not support them, tear down the session
#			if family not in message.capabilities[CapabilityID.MULTIPROTOCOL_EXTENSIONS]:
#				afi,safi = family
#				raise Notify(2,0,'Peers does not speak %s %s' % (afi,safi))

		logger.message(self.me('<< %s' % message))
		return message

	def read_keepalive (self):
		message = self.read_message()
		if message.TYPE == NOP.TYPE:
			return message
		if message.TYPE != KeepAlive.TYPE:
			raise Notify(5,2)
		logger.message(self.me('<< KEEPALIVE (ESTABLISHED)'))
		return message

	# Sending message to peer .................................................

	# we do not buffer those message in purpose

	def new_open (self,restarted,asn4):
		if asn4:
			asn = self.neighbor.local_as
		else:
			asn = AS_TRANS

		o = Open(4,asn,self.neighbor.router_id.ip,Capabilities().default(self.neighbor,restarted),self.neighbor.hold_time)

		if not self.connection.write(o.message()):
			raise Failure('Could not send open')
		logger.message(self.me('>> %s' % o))
		return o

	def new_keepalive (self,force=None):
		left = int(self.connection.last_write + self.hold_time.keepalive() - time.time())
		k = KeepAlive()
		m = k.message()
		if force:
			written = self.connection.write(k.message())
			if not written:
				logger.message(self.me('|| buffer not yet empty, adding KEEPALIVE to it'))
				self._messages.append((1,'KEEPALIVE',m))
			else:
				self._frozen = 0
				if force == True:
					logger.message(self.me('>> KEEPALIVE (OPENCONFIRM)'))
				elif force == False:
					logger.message(self.me('>> KEEPALIVE (no more UPDATE and no EOR)'))
			return left,k
		if left <= 0:
			written = self.connection.write(k.message())
			if not written:
				logger.message(self.me('|| could not send KEEPALIVE, buffering'))
				self._messages.append((1,'KEEPALIVE',m))
			else:
				logger.message(self.me('>> KEEPALIVE'))
				self._frozen = 0
			return left,k
		return left,None

	def new_notification (self,notification):
		return self.connection.write(notification.message())

	def clear_buffer (self):
		logger.message(self.me('clearing MESSAGE(s) buffer'))
		self._messages = []

	def buffered (self):
		return len(self._messages)

	def _backlog (self):
		# performance only to remove inference
		if self._messages:
			if not self._frozen:
				self._frozen = time.time()
			if self._frozen and self._frozen + (self.hold_time) < time.time():
				raise Failure('peer %s not reading on socket - killing session' % self.neighbor.peer_as)
			logger.message(self.me("unable to send route for %d second (maximum allowed %d)" % (time.time()-self._frozen,self.hold_time)))
			nb_backlog = len(self._messages)
			if nb_backlog > MAX_BACKLOG:
				raise Failure('over %d chunked routes buffered for peer %s - killing session' % (MAX_BACKLOG,self.neighbor.peer_as))
			logger.message(self.me("self._messages of %d/%d chunked routes" % (nb_backlog,MAX_BACKLOG)))
		while self._messages:
			number,name,update = self._messages[0]
			if not self.connection.write(update):
				logger.message(self.me("|| failed to send %d %s(s) from buffer" % (number,name)))
				break
			self._messages.pop(0)
			self._frozen = 0
			yield number

	def _announce (self,name,generator):
		def chunked (generator,size):
			chunk = ''
			number = 0
			for data in generator:
				if len(data) > size:
					raise Failure('Can not send BGP update larger than %d bytes on this connection.' % size)
				if len(chunk) + len(data) <= size:
					chunk += data
 					number += 1
					continue
				yield number,chunk
				chunk = data
				number = 1
			if chunk:
				yield number,chunk

		if self._messages:
			# The message size is the whole BGP message INCLUDING headers !
			for number,update in chunked(generator,self.message_size-19):
					logger.message(self.me('|| adding %d  %s(s) to existing buffer' % (number,name)))
					self._messages.append((number,name,update))
			for number in self._backlog():
				logger.message(self.me('>> %d buffered %s(s)' % (number,name)))
				yield number
		else:
			sending = True
			# The message size is the whole BGP message INCLUDING headers !
			for number,update in chunked(generator,self.message_size-19):
				if sending:
					if self.connection.write(update):
						logger.message(self.me('>> %d %s(s)' % (number,name)))
						yield number
					else:
						logger.message(self.me('|| could not send %d %s(s), buffering' % (number,name)))
						self._messages.append((number,name,update))
						sending = False
				else:
					logger.message(self.me('|| buffering the rest of the %s(s) (%d)' % (name,number)))
					self._messages.append((number,name,update))
					yield 0

	def new_update (self):
		# XXX: This should really be calculated once only
		asn4 = not not self.peer.open.capabilities.announced(CapabilityID.FOUR_BYTES_ASN)
		for number in self._announce('UPDATE',self._delta.updates(asn4,self.neighbor.local_as,self.neighbor.peer_as,self.use_path)):
			yield number

	def new_eors (self,families):
		eor = EOR()
		eors = eor.eors(families)
		for answer in self._announce('EOR %s' % ', '.join(['%s %s' % (str(afi),str(safi)) for (afi,safi) in families]),eors):
			pass

	# Message Factory .................................................

	def KeepAliveFactory (self,data):
		return KeepAlive()

	def _key_values (self,name,data):
		if len(data) < 2:
			raise Notify(2,0,"Bad length for OPEN %s (<2) %s" % (name,hexa(data)))
		l = ord(data[1])
		boundary = l+2
		if len(data) < boundary:
			raise Notify(2,0,"Bad length for OPEN %s (buffer underrun) %s" % (name,hexa(data)))
		key = ord(data[0])
		value = data[2:boundary]
		rest = data[boundary:]
		return key,value,rest

	def CapabilitiesFactory (self,data):
		capabilities = Capabilities()
		option_len = ord(data[0])
		if option_len:
			data = data[1:]
			while data:
				key,value,data = self._key_values('parameter',data)
				# Paramaters must only be sent once.
				if key == Parameter.AUTHENTIFICATION_INFORMATION:
					raise Notify(2,5)

				if key == Parameter.CAPABILITIES:
					while value:
						k,capv,value = self._key_values('capability',value)
						# Multiple Capabilities can be present in a single attribute
						#if r:
						#	raise Notify(2,0,"Bad length for OPEN %s (size mismatch) %s" % ('capability',hexa(value)))

						if k == CapabilityID.MULTIPROTOCOL_EXTENSIONS:
							if k not in capabilities:
								capabilities[k] = MultiProtocol()
							afi = AFI(unpack('!H',capv[:2])[0])
							safi = SAFI(ord(capv[3]))
							capabilities[k].append((afi,safi))
							continue

						if k == CapabilityID.GRACEFUL_RESTART:
							restart = unpack('!H',capv[:2])[0]
							restart_flag = restart >> 12
							restart_time = restart & Graceful.TIME_MASK
							value_gr = capv[2:]
							families = []
							while value_gr:
								afi = AFI(unpack('!H',value_gr[:2])[0])
								safi = SAFI(ord(value_gr[2]))
								flag_family = ord(value_gr[0])
								families.append((afi,safi,flag_family))
								value_gr = value_gr[4:]
							capabilities[k] = Graceful(restart_flag,restart_time,families)
							continue

						if k == CapabilityID.FOUR_BYTES_ASN:
							capabilities[k] = ASN(unpack('!L',capv[:4])[0])
							continue

						if k == CapabilityID.ROUTE_REFRESH:
							capabilities[k] = RouteRefresh()
							continue

						if k == CapabilityID.CISCO_ROUTE_REFRESH:
							capabilities[k] = CiscoRouteRefresh()
							continue

						if k == CapabilityID.MULTISESSION_BGP:
							capabilities[k] = MultiSession()
							continue
						if k == CapabilityID.MULTISESSION_BGP_RFC:
							capabilities[k] = MultiSession()
							continue
						if k == CapabilityID.ADD_PATH:
							capabilities[k] = AddPath()
							value_ad = capv
							while value_ad:
								afi = AFI(unpack('!H',value_ad[:2])[0])
								safi = SAFI(ord(value_ad[2]))
								sr = ord(value_ad[3])
								capabilities[k].add_path(afi,safi,sr)
								value_ad = value_ad[4:]

						if k not in capabilities:
							capabilities[k] = Unknown(k,[ord(_) for _ in capv])
				else:
					raise Notify(2,0,'Unknow OPEN parameter %s' % hex(key))
		return capabilities

	def OpenFactory (self,data):
		version = ord(data[0])
		if version != 4:
			# Only version 4 is supported nowdays..
			raise Notify(2,1,data[0])
		asn = unpack('!H',data[1:3])[0]
		hold_time = unpack('!H',data[3:5])[0]
		numeric = unpack('!L',data[5:9])[0]
		router_id = "%d.%d.%d.%d" % (numeric>>24,(numeric>>16)&0xFF,(numeric>>8)&0xFF,numeric&0xFF)
		capabilities = self.CapabilitiesFactory(data[9:])
		return Open(version,asn,router_id,capabilities,hold_time)


	def UpdateFactory (self,data):
		length = len(data)
		# withdraw
		lw,withdrawn,data = defix(data)
		if len(withdrawn) != lw:
			raise Notify(3,1,'invalid withdrawn routes length, not enough data available')
		la,attribute,announced = defix(data)
		if len(attribute) != la:
			raise Notify(3,1,'invalid total path attribute length, not enough data available')
		# The RFC check ...
		#if lw + la + 23 > length:
		if 2 + lw + 2+ la + len(announced) != length:
			raise Notify(3,1,'error in BGP message lenght, not enough data for the size announced')

		# Is the peer going to send us some Path Information with the route (AddPath)
		path_info = self.use_path.receive(AFI(AFI.ipv4),SAFI(SAFI.unicast))

		self.mp_announce = []
		self.mp_withdraw = []
		attributes = self.AttributesFactory(attribute)

		routes = []
		while withdrawn:
			nlri = BGPNLRI(AFI.ipv4,SAFI.unicast_multicast,withdrawn,path_info)
			route = RouteBGP(nlri,'withdrawn')
			route.attributes = self.attributes
			withdrawn = withdrawn[len(nlri):]
			routes.append(route)

		while announced:
			nlri = BGPNLRI(AFI.ipv4,SAFI.unicast_multicast,announced,path_info)
			route = RouteBGP(nlri,'announced')
			route.attributes = attributes
			announced = announced[len(nlri):]
			routes.append(route)

		for route in self.mp_withdraw:
			route.attributes = attributes
			routes.append(route)

		for route in self.mp_announce:
			route.attributes = attributes
			routes.append(route)

		if routes:
			return Update(routes)
		return NOP('')

	def __merge_attributes (self):
		as2path = self.attributes[AttributeID.AS_PATH]
		as4path = self.attributes[AttributeID.AS4_PATH]
		self.attributes.remove(AttributeID.AS_PATH)
		self.attributes.remove(AttributeID.AS4_PATH)

		# this key is unique as index length is a two header, plus a number of ASN of size 2 or 4
		# so adding the : make the length odd and unique
		key = "%s:%s" % (as2path.index, as4path.index)
		
		# found a cache copy
		if self.attributes.get(AttributeID.AS_PATH,key):
			return

		as_seq = []
		as_set = []

		len2 = len(as2path.as_seq)
		len4 = len(as4path.as_seq)

		# RFC 4893 section 4.2.3
		if len2 < len4:
			as_seq = as2path.as_seq
		else:
			as_seq = as2path.as_seq[:-len4]
			as_seq.extend(as4path.as_seq)

		len2 = len(as2path.as_set)
		len4 = len(as4path.as_set)

		if len2 < len4:
			as_set = as4path.as_set
		else:
			as_set = as2path.as_set[:-len4]
			as_set.extend(as4path.as_set)

		aspath = ASPath(as_seq,as_set)
		self.attributes.add(aspath,key)


	def AttributesFactory (self,data):
		try:
			self.attributes = Attributes()
			self._AttributesFactory(data)
			if AttributeID.AS_PATH in self.attributes and AttributeID.AS4_PATH in self.attributes:
				self.__merge_attributes()
			return self.attributes
		except IndexError:
			raise Notify(3,2,data)

	def __new_communities (self,data):
		communities = Communities()
		while data:
			if data and len(data) < 4:
				raise Notify(3,1,'could not decode community %s' % str([hex(ord(_)) for _ in data]))
			communities.add(Community(data[:4]))
			data = data[4:]
		return communities

	def __new_extended_communities (self,data):
		communities = ECommunities()
		while data:
			if data and len(data) < 8:
				raise Notify(3,1,'could not decode extended community %s' % str([hex(ord(_)) for _ in data]))
			communities.add(ECommunity(data[:8]))
			data = data[8:]
		return communities

	def __new_aspaths (self,data,asn4,klass):
		as_set = []
		as_seq = []
		backup = data

		unpacker = {
			False : '!H',
			True  : '!L',
		}
		size = {
			False: 2,
			True : 4,
		}
		as_choice = {
			ASPath.AS_SEQUENCE : as_seq,
			ASPath.AS_SET      : as_set,
		}

		upr = unpacker[asn4]
		length = size[asn4]

		try:

			while data:
				stype = ord(data[0])
				slen  = ord(data[1])

				if stype not in (ASPath.AS_SET, ASPath.AS_SEQUENCE):
					raise Notify(3,11,'invalid AS Path type sent %d' % stype)

				end = 2+(slen*length)
				sdata = data[2:end]
				data = data[end:]
				asns = as_choice[stype]

				for i in range(slen):
					asn = unpack(upr,sdata[:length])[0]
					asns.append(ASN(asn))
					sdata = sdata[length:]

		except IndexError:
			raise Notify(3,11,'not enough data to decode AS_PATH or AS4_PATH')
		except error: # struct
			raise Notify(3,11,'not enough data to decode AS_PATH or AS4_PATH')

		return klass(as_seq,as_set,backup)

	def __new_ASPath (self,data,asn4):
		return self.__new_aspaths(data,asn4,ASPath)

	def __new_ASPath4 (self,data):
		return self.__new_aspaths(data,True,AS4Path)

	def _AttributesFactory (self,data):
		if not data:
			return self

		# We do not care if the attribute are transitive or not as we do not redistribute
		flag = Flag(ord(data[0]))
		code = AttributeID(ord(data[1]))

		if flag & Flag.EXTENDED_LENGTH:
			length = unpack('!H',data[2:4])[0]
			offset = 4
		else:
			length = ord(data[2])
			offset = 3

		data = data[offset:]
		next = data[length:]
		attribute = data[:length]

		logger.parser(LazyFormat("parsing %s " % code,hexa,data[:length]))

		if code == AttributeID.ORIGIN:
			if not self.attributes.get(code,attribute):
				self.attributes.add(Origin(ord(attribute)),attribute)
			return self._AttributesFactory(next)

		# only 2-4% of duplicated data - is it worth to cache ?
		if code == AttributeID.AS_PATH:
			if length:
				# we store the AS4_PATH as AS_PATH, do not over-write
				if not self.attributes.has(code):
					if not self.attributes.get(code,attribute):
						self.attributes.add(self.__new_ASPath(attribute,self._asn4),attribute)
			return self._AttributesFactory(next)

		if code == AttributeID.AS4_PATH:
			if length:
				# ignore the AS4_PATH on new spekers as required by RFC 4893 section 4.1
				if not self._asn4 and not self.attributes.get(code,attribute):
					# This replace the old AS_PATH
					self.attributes.add(self.__new_ASPath4(attribute),attribute)
			return self._AttributesFactory(next)

		if code == AttributeID.NEXT_HOP:
			if not self.attributes.get(code,attribute):
				self.attributes.add(NextHop(AFI.ipv4,SAFI.unicast_multicast,attribute),attribute)
			return self._AttributesFactory(next)

		if code == AttributeID.MED:
			if not self.attributes.get(code,attribute):
				self.attributes.add(MED(attribute),attribute)
			return self._AttributesFactory(next)

		if code == AttributeID.LOCAL_PREF:
			if not self.attributes.get(code,attribute):
				self.attributes.add(LocalPreference(attribute),attribute)
			return self._AttributesFactory(next)

		if code == AttributeID.ATOMIC_AGGREGATE:
			if not self.attributes.get(AttributeID.ATOMIC_AGGREGATE,attribute):
				raise Notify(3,2,'invalid ATOMIC_AGGREGATE %s' % [hex(ord(_)) for _ in attribute])
			return self._AttributesFactory(next)

		if code == AttributeID.AGGREGATOR:
			# AS4_AGGREGATOR are stored as AGGREGATOR - so do not overwrite if exists
			if not self.attributes.has(code):
				if not self.attributes.get(code,attribute):
					self.attributes.add(Aggregator(attribute),attribute)
			return self._AttributesFactory(next)

		if code == AttributeID.AS4_AGGREGATOR:
			if not self.attributes.get(AttributeID.AGGREGATOR,attribute):
				self.attributes.add(Aggregator(attribute),attribute)
			return self._AttributesFactory(next)

		if code == AttributeID.COMMUNITY:
			if not self.attributes.get(code,attribute):
				self.attributes.add(self.__new_communities(attribute),attribute)
			return self._AttributesFactory(next)

		if code == AttributeID.ORIGINATOR_ID:
			if not self.attributes.get(code,attribute):
				self.attributes.add(OriginatorID(AFI.ipv4,SAFI.unicast,data[:4]),attribute)
			return self._AttributesFactory(next)

		if code == AttributeID.CLUSTER_LIST:
			if not self.attributes.get(code,attribute):
				self.attributes.add(ClusterList(attribute),attribute)
			return self._AttributesFactory(next)

		if code == AttributeID.EXTENDED_COMMUNITY:
			if not self.attributes.get(code,attribute):
				self.attributes.add(self.__new_extended_communities(attribute),attribute)
			return self._AttributesFactory(next)

		if code == AttributeID.MP_UNREACH_NLRI:
			# -- Reading AFI/SAFI
			data = data[:length]
			afi,safi = unpack('!HB',data[:3])
			offset = 3
			data = data[offset:]

			if (afi,safi) not in self.neighbor.families():
				raise Notify(3,0,'presented a non-negociated family')

			# Is the peer going to send us some Path Information with the route (AddPath)
			path_info = self.use_path.receive(afi,safi)
			while data:
				route = RouteBGP(BGPNLRI(afi,safi,data,path_info),'withdrawn')
				route.attributes = self.attributes
				self.mp_withdraw.append(route)
				data = data[len(route.nlri):]
			return self._AttributesFactory(next)

		if code == AttributeID.MP_REACH_NLRI:
			data = data[:length]
			# -- Reading AFI/SAFI
			afi,safi = unpack('!HB',data[:3])
			offset = 3

			# we do not want to accept unknown families
			if (afi,safi) not in self.neighbor.families():
				raise Notify(3,0,'presented a non-negociated family')

			# -- Reading length of next-hop
			len_nh = ord(data[offset])
			offset += 1

			rd = 0

			# check next-hope size
			if afi == AFI.ipv4:
				if safi in (SAFI.unicast,SAFI.multicast):
					if len_nh != 4:
						raise Notify(3,0,'invalid next-hop length')
				if safi in (SAFI.mpls_vpn,):
					if len_nh != 12:
						raise Notify(3,0,'invalid next-hop length')
					rd = 8
				size = 4
			elif afi == AFI.ipv6:
				if safi in (SAFI.unicast,):
					if len_nh not in (16,32):
						raise Notify(3,0,'invalid next-hop length')
				if safi in (SAFI.mpls_vpn,):
					if len_nh not in (24,40):
						raise Notify(3,0,'invalid next-hop length')
					rd = 8
				size = 16

			# -- Reading next-hop
			nh = data[offset+rd:offset+rd+size]

			# chech the RD is well zeo
			if rd and sum([int(ord(_)) for _ in data[offset:8]]) != 0:
				raise Notify(3,0,'route-distinguisher for the next-hop is not zero')

			offset += len_nh

			# Skip a reserved bit as somone had to bug us !
			reserved = ord(data[offset])
			offset += 1

			if reserved != 0:
				raise Notify(3,0,'the reserved bit of MP_REACH_NLRI is not zero')

			# Is the peer going to send us some Path Information with the route (AddPath)
			path_info = self.use_path.receive(afi,safi)

			# Reading the NLRIs
			data = data[offset:]

			while data:
				route = RouteBGP(BGPNLRI(afi,safi,data,path_info),'announced')
				if not route.attributes.get(AttributeID.NEXT_HOP,nh):
					route.attributes.add(NextHop(afi,safi,nh),nh)
				self.mp_announce.append(route)
				data = data[len(route.nlri):]
			return self._AttributesFactory(next)

		logger.parser('ignoring attribute')
		return self._AttributesFactory(next)
