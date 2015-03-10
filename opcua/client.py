from threading import Thread, Condition
import logging
import uuid

from opcua import uaprotocol as ua
from opcua import BinaryClient, Node, Subscription
from urllib.parse import urlparse

class KeepAlive(Thread):
    """
    Used by Client to keep session opened.
    OPCUA defines timeout both for sessions and secure channel
    """
    def __init__(self, client, timeout):
        Thread.__init__(self)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.timeout = timeout
        self.client = client
        self._dostop = False
        self._cond = Condition()

    def run(self):
        self.logger.debug("starting keepalive thread")
        server_state = self.client.get_node(ua.FourByteNodeId(ua.ObjectIds.Server_ServerStatus_State))
        while not self._dostop:
            with self._cond:
                self._cond.wait(self.timeout)
            if self._dostop:
                break
            self.logger.debug("renewing channel")
            self.client.open_secure_channel(renew=True)
            val = server_state.get_value()
            self.logger.debug("server state is: %s ", val)
        self.logger.debug("keepalive thread has stopped")

    def stop(self):
        self.logger.debug("stoping keepalive thread")
        with self._cond:
            self._cond.notify_all()
        self._dostop = True


class Client(object):
    """
    High level client to connect to an OPC-UA server. 
    This class makes it easy to connect and browse address space.
    It attemps to expose as much functionality as possible
    but if you want to do to special things you will probably need
    to work with the BinaryClient object, available as self.bclient
    which offers a raw OPC-UA interface.

    """
    def __init__(self, url):
        """
        used url argument to connect to server.
        if you are unsure of url, write at least hostname and port
        and call get_endpoints
        """
        self.logger = logging.getLogger(self.__class__.__name__)
        self.server_url = urlparse(url)
        self.name = "Pure Python Client" 
        self.description = self.name 
        self.application_uri = "urn:freeopcua:client"
        self.product_uri = "urn:freeopcua.github.no:client"
        self.security_policy_uri = "http://opcfoundation.org/UA/SecurityPolicy#None"
        self.secure_channel_id = None
        self.default_timeout = 3600000
        self.bclient = BinaryClient()
        self._nonce = None
        self._session_counter = 1
        self.keepalive = None

    def get_server_endpoints(self):
        """
        Connect, ask server for endpoints, and disconnect
        """
        self.connect_socket()
        self.send_hello()
        self.open_secure_channel()
        endpoints = self.get_endpoints()
        self.close_secure_channel()
        return endpoints

    def connect(self):
        """
        High level method
        Connect, create and activate session
        """
        self.connect_socket()
        self.send_hello()
        self.open_secure_channel()
        self.create_session()
        self.activate_session()

    def disconnect(self):
        """
        High level method
        Close session, secure channel and socket
        """
        self.close_session()
        self.close_secure_channel()
        self.disconnect_socket()

    def connect_socket(self):
        """
        connect to socket defined in url
        """
        self.bclient.connect_socket(self.server_url.hostname, self.server_url.port)

    def disconnect_socket(self):
        self.bclient.disconnect_socket()

    def send_hello(self):
        """
        Send OPC-UA hello to server
        """
        ack = self.bclient.send_hello(self.server_url.geturl())
        #FIXME check ack

    def open_secure_channel(self, renew=False):
        """
        Open secure channel, if renew is True, renew channel
        """
        params = ua.OpenSecureChannelParameters()
        params.ClientProtocolVersion = 0
        params.RequestType = ua.SecurityTokenRequestType.Issue
        if renew:
            params.RequestType = ua.SecurityTokenRequestType.Renew
        params.SecurityMode = ua.MessageSecurityMode.None_
        params.RequestedLifetime = 300000
        params.ClientNonce = '\x00'
        self.bclient.open_secure_channel(params)

    def close_secure_channel(self):
        return self.bclient.close_secure_channel()

    def get_endpoints(self):
        params = ua.GetEndpointsParameters()
        params.EndpointUrl = self.server_url.geturl()
        params.ProfileUris = ["http://opcfoundation.org/UA-Profile/Transport/uatcp-uasc-uabinary"]
        params.LocaleIds = ["http://opcfoundation.org/UA-Profile/Transport/uatcp-uasc-uabinary"]
        return self.bclient.get_endpoints(params)

    def create_session(self):
        desc = ua.ApplicationDescription()
        desc.ApplicationUri = self.application_uri
        desc.ProductUri = self.product_uri
        desc.ApplicationName = ua.LocalizedText(self.name)
        desc.ApplicationType = ua.ApplicationType.Client

        params = ua.CreateSessionParameters()
        params.ClientNonce = uuid.uuid4().bytes + uuid.uuid4().bytes #seems we need at least 32 bytes not 16 as python gives us...
        params.ClientCertificate = b''
        params.ClientDescription = desc 
        params.EndpointUrl = self.server_url.geturl()
        params.SessionName = self.description + " Session" + str(self._session_counter)
        params.RequestedSessionTimeout = 3600000
        params.MaxResponseMessageSize = 0 #means not max size
        response = self.bclient.create_session(params)
        self.keepalive = KeepAlive(self, response.RevisedSessionTimeout * 0.8)
        self.keepalive.start()
        return response

    def activate_session(self):
        params = ua.ActivateSessionParameters()
        params.LocaleIds.append("en")
        params.UserIdentityToken = ua.AnonymousIdentityToken()
        params.UserIdentityToken.PolicyId = b"anonymous"
        return self.bclient.activate_session(params)

    def close_session(self):
        """
        Close session
        """
        if self.keepalive:
            self.keepalive.stop()
        return self.bclient.close_session(True)

    def get_root_node(self):
        return self.get_node(ua.TwoByteNodeId(ua.ObjectIds.RootFolder))

    def get_objects_node(self):
        return self.get_node(ua.TwoByteNodeId(ua.ObjectIds.ObjectsFolder))

    def get_node(self, nodeid):
        """
        Get node using NodeId object or a string representing a NodeId
        """
        return Node(self.bclient, nodeid)

    def create_subscription(self, period, handler):
        """
        Create a subscription.
        returns a Subscription object which allow
        to subscribe to events or data on server
        """
        params = ua.CreateSubscriptionParameters()
        params.RequestedPublishingInterval = period
        params.RequestedLifetimeCount = 3000
        params.RequestedMaxKeepAliveCount = 10000
        params.MaxNotificationsPerPublish = 0
        params.PublishingEnabled = True
        params.Priority = 0
        return Subscription(self.bclient, params, handler)



