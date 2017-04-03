#!/usr/bin/env python
__author__ = 'Vahid Jalili'

from future.backports.urllib.parse import parse_qs

import json
import os
import re
import sys
import traceback
import argparse
import importlib
from mako.lookup import TemplateLookup

from oic.oic.provider import AuthorizationEndpoint
from oic.oic.provider import EndSessionEndpoint
from oic.oic.provider import Provider
from oic.oic.provider import RegistrationEndpoint
from oic.oic.provider import TokenEndpoint
from oic.oic.provider import UserinfoEndpoint
from oic.utils import shelve_wrapper
from oic.utils.authn.authn_context import AuthnBroker
from oic.utils.authn.authn_context import make_auth_verify
from oic.utils.authn.client import verify_client
from oic.utils.authn.javascript_login import JavascriptFormMako
from oic.utils.authn.multi_auth import AuthnIndexedEndpointWrapper
from oic.utils.authn.multi_auth import setup_multi_auth
from oic.utils.authn.saml import SAMLAuthnMethod
from oic.utils.authn.user import UsernamePasswordMako
from oic.utils.authz import AuthzHandling
from oic.utils.http_util import *
from oic.utils.keyio import keyjar_init
from oic.utils.userinfo import UserInfo
from oic.utils.userinfo.aa_info import AaUserInfo
from oic.utils.webfinger import OIC_ISSUER
from oic.utils.webfinger import WebFinger
from oic.utils.sdb import SessionDB


from cherrypy import wsgiserver
from cherrypy.wsgiserver.ssl_builtin import BuiltinSSLAdapter

from oic.utils.sdb import SessionDB



LOGGER = logging.getLogger("")
LOGFILE_NAME = 'oc.log'
hdlr = logging.FileHandler(LOGFILE_NAME)
base_formatter = logging.Formatter(
    "%(asctime)s %(name)s:%(levelname)s %(message)s")

CPC = ('%(asctime)s %(name)s:%(levelname)s '
       '[%(client)s,%(path)s,%(cid)s] %(message)s')
cpc_formatter = logging.Formatter(CPC)

hdlr.setFormatter(base_formatter)
LOGGER.addHandler(hdlr)
LOGGER.setLevel(logging.DEBUG)

logger = logging.getLogger('oicServer')


def static_file(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


# noinspection PyUnresolvedReferences
def static(self, environ, start_response, path):
    logger.info("[static]sending: %s" % (path,))

    try:
        data = open(path, 'rb').read()
        if path.endswith(".ico"):
            start_response('200 OK', [('Content-Type', "image/x-icon")])
        elif path.endswith(".html"):
            start_response('200 OK', [('Content-Type', 'text/html')])
        elif path.endswith(".json"):
            start_response('200 OK', [('Content-Type', 'application/json')])
        elif path.endswith(".txt"):
            start_response('200 OK', [('Content-Type', 'text/plain')])
        elif path.endswith(".css"):
            start_response('200 OK', [('Content-Type', 'text/css')])
        else:
            start_response('200 OK', [('Content-Type', "text/xml")])
        return [data]
    except IOError:
        resp = NotFound()
        return resp(environ, start_response)


def check_session_iframe(self, environ, start_response, logger):
    return static(self, environ, start_response, "htdocs/op_session_iframe.html")


def key_rollover(self, environ, start_response, _):
    # expects a post containing the necessary information
    _txt = get_post(environ)
    _jwks = json.loads(_txt)
    #logger.info("Key rollover to")
    provider.do_key_rollover(_jwks, "key_%d_%%d" % int(time.time()))
    # Dump to file
    f = open(jwksFileName, "w")
    f.write(json.dumps(provider.keyjar.export_jwks()))
    f.close()
    resp = Response("OK")
    return resp(environ, start_response)


def clear_keys(self, environ, start_response, _):
    provider.remove_inactive_keys()
    resp = Response("OK")
    return resp(environ, start_response)


class Application(object):
    def __init__(self, oas, urls):
        self.oas = oas

        self.endpoints = [
            AuthorizationEndpoint(self.authorization),
            TokenEndpoint(self.token),
            UserinfoEndpoint(self.userinfo),
            RegistrationEndpoint(self.registration),
            EndSessionEndpoint(self.endsession),
        ]

        self.oas.endpoints = self.endpoints
        self.urls = urls
        self.urls.extend([
            (r'^.well-known/openid-configuration', self.op_info),
            (r'^.well-known/simple-web-discovery', self.swd_info),
            (r'^.well-known/host-meta.json', self.meta_info),
            (r'^.well-known/webfinger', self.webfinger),
            #    (r'^.well-known/webfinger', webfinger),
            (r'.+\.css$', self.css),
            (r'safe', self.safe),
            (r'^keyrollover', key_rollover),
            (r'^clearkeys', clear_keys),
            (r'^check_session', check_session_iframe)
            #    (r'tracelog', trace_log),
        ])

        self.add_endpoints(self.endpoints)

    def add_endpoints(self, extra):
        for endp in extra:
            self.urls.append(("^%s" % endp.etype, endp.func))

    # noinspection PyUnusedLocal
    def safe(self, environ, start_response):
        _srv = self.oas.server
        _log_info = self.oas.logger.info

        _log_info("- safe -")
        # _log_info("env: %s" % environ)
        # _log_info("handle: %s" % (handle,))

        try:
            authz = environ["HTTP_AUTHORIZATION"]
            (typ, code) = authz.split(" ")
            assert typ == "Bearer"
        except KeyError:
            resp = BadRequest("Missing authorization information")
            return resp(environ, start_response)

        try:
            _sinfo = _srv.sdb[code]
        except KeyError:
            resp = Unauthorized("Not authorized")
            return resp(environ, start_response)

        info = "'%s' secrets" % _sinfo["sub"]
        resp = Response(info)
        return resp(environ, start_response)

    # noinspection PyUnusedLocal
    def css(self, environ, start_response):
        try:
            info = open(environ["PATH_INFO"]).read()
            resp = Response(info)
        except (OSError, IOError):
            resp = NotFound(environ["PATH_INFO"])

        return resp(environ, start_response)

    # noinspection PyUnusedLocal
    def token(self, environ, start_response):
        return wsgi_wrapper(environ, start_response, self.oas.token_endpoint,
                            logger=logger)

    # noinspection PyUnusedLocal
    def authorization(self, environ, start_response):
        return wsgi_wrapper(environ, start_response,
                            self.oas.authorization_endpoint, logger=logger)  # cookies required.

    # noinspection PyUnusedLocal
    def userinfo(self, environ, start_response):
        print '\n in userinfo'
        return wsgi_wrapper(environ, start_response, self.oas.userinfo_endpoint,
                            logger=logger)

    # noinspection PyUnusedLocal
    def op_info(self, environ, start_response):
        return wsgi_wrapper(environ, start_response,
                            self.oas.providerinfo_endpoint, logger=logger)

    # noinspection PyUnusedLocal
    def registration(self, environ, start_response):
        if environ["REQUEST_METHOD"] == "POST":
            return wsgi_wrapper(environ, start_response,
                                self.oas.registration_endpoint,
                                logger=logger)
        elif environ["REQUEST_METHOD"] == "GET":
            return wsgi_wrapper(environ, start_response,
                                self.oas.read_registration, logger=logger)
        else:
            resp = ServiceError("Method not supported")
            return resp(environ, start_response)

    # noinspection PyUnusedLocal
    def check_id(self, environ, start_response):
        return wsgi_wrapper(environ, start_response, self.oas.check_id_endpoint,
                            logger=logger)

    # noinspection PyUnusedLocal
    def swd_info(self, environ, start_response):
        return wsgi_wrapper(environ, start_response, self.oas.discovery_endpoint,
                            logger=logger)

    # noinspection PyUnusedLocal
    def trace_log(self, environ, start_response):
        return wsgi_wrapper(environ, start_response, self.oas.tracelog_endpoint,
                            logger=logger)

    # noinspection PyUnusedLocal
    def endsession(self, environ, start_response):
        return wsgi_wrapper(environ, start_response,
                            self.oas.endsession_endpoint, logger=logger)

    # noinspection PyUnusedLocal
    def meta_info(self, environ, start_response):
        """
        Returns something like this::

             {"links":[
                 {
                    "rel":"http://openid.net/specs/connect/1.0/issuer",
                    "href":"https://openidconnect.info/"
                 }
             ]}

        """
        print '\n in meta-info'
        pass

    def webfinger(self, environ, start_response):
        query = parse_qs(environ["QUERY_STRING"])
        try:
            assert query["rel"] == [OIC_ISSUER]
            resource = query["resource"][0]
        except KeyError:
            resp = BadRequest("Missing parameter in request")
        else:
            wf = WebFinger()
            resp = Response(wf.response(subject=resource,
                                        base=self.oas.baseurl))
        return resp(environ, start_response)

    def application(self, environ, start_response):
        """
        The main WSGI application. Dispatch the current request to
        the functions from above and store the regular expression
        captures in the WSGI environment as  `oic.url_args` so that
        the functions from above can access the url placeholders.

        If nothing matches call the `not_found` function.

        :param environ: The HTTP application environment
        :param start_response: The application to run when the handling of the
            request is done
        :return: The response as a list of lines
        """
        # user = environ.get("REMOTE_USER", "")
        path = environ.get('PATH_INFO', '').lstrip('/')

        if path == "robots.txt":
            return static(self, environ, start_response, "static/robots.txt")

        environ["oic.oas"] = self.oas

        # logger.info('PATH: "{}"'.format(path))

        if path.startswith("static/"):
            return static(self, environ, start_response, path)

        for regex, callback in self.urls:
            match = re.search(regex, path)
            if match is not None:
                try:
                    environ['oic.url_args'] = match.groups()[0]
                except IndexError:
                    environ['oic.url_args'] = path

                #logger.info("callback: %s" % callback)
                try:
                    return callback(environ, start_response)
                except Exception as err:
                    print("%s" % err)
                    message = traceback.format_exception(*sys.exc_info())
                    print(message)
                    logger.exception("%s" % err)
                    resp = ServiceError("%s" % err)
                    return resp(environ, start_response)

        LOGGER.debug("unknown side: %s" % path)
        resp = NotFound("Couldn't find the side you asked for!")
        return resp(environ, start_response)


if __name__ == '__main__':

    root = './'
    lookup = TemplateLookup(directories=[root + 'Templates', root + 'htdocs'],
                            module_directory=root + 'modules',
                            input_encoding='utf-8', output_encoding='utf-8')
    kwargs = {
        "template_lookup": lookup,
        "template": {"form_post": "form_response.mako"}
    }

    usernamePasswords = {
        "user1": "1",
        "user2": "2"
    }

    passwordEndPointIndex = 0  # what is this, and what does its value mean?

    # JWKS: JSON Web Key
    jwksFileName = "static/jwks.json"

    # parse the parameters
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', dest='config')
    parser.add_argument('-d', dest='debug', action='store_true')
    args = parser.parse_args()

    # parse and setup configuration
    config = importlib.import_module(args.config)
    config.ISSUER = config.ISSUER + ':{}/'.format(config.PORT)
    config.SERVICEURL = config.SERVICEURL.format(issuer=config.ISSUER)
    endPoints = config.AUTHENTICATION["UserPassword"]["END_POINTS"]
    fullEndPointsPath = ["%s%s" % (config.ISSUER, ep) for ep in endPoints]

# TODO: why this instantiation happens so early? can I move it later?
    # An OIDC Authorization/Authentication server is designed to
    # allow more than one authentication method to be used by the server.
    # And that is what the AuthBroker is for.
    # Given information about the authorisation request, the AuthBroker
    # chooses which method(s) to be used for authenticating the person/entity.
    # According to the OIDC standard the Relaying Party can say
    # 'I want this type of authentication'. The AuthnBroker tries to pick
    # methods from the set it has been supplied, to map that request.
    authnBroker = AuthnBroker()

    # UsernamePasswordMako: authenticas a user using the username/password form in a
    # WSGI environment using Mako as template system
    usernamePasswordAuthn = UsernamePasswordMako(
        None,                               # server instance
        "login.mako",                       # a mako template
        lookup,                             # lookup template
        usernamePasswords,                  # username/password dictionary-like database
        "%sauthorization" % config.ISSUER,  # where to send the user after authentication
        None,                               # templ_arg_func ??!!
        fullEndPointsPath)                  # verification endpoints

    # AuthnIndexedEndpointWrapper is a wrapper class for using an authentication module with multiple endpoints.
    authnIndexedEndPointWrapper = AuthnIndexedEndpointWrapper(usernamePasswordAuthn, passwordEndPointIndex)

# TODO: end_point is used in _urls only, and _urls are used toward the end of this code,
# TODO: so it is possible to push these to closet usage point?
    # END_POINT is defined as a dictionary in the configuration file,
    # why not defining it as string with "verify" value?
    # after all, we have only one end point.
    # can we have multiple end points for password? why?
    endPoint = config.AUTHENTICATION["UserPassword"]["END_POINTS"][passwordEndPointIndex]
    # what are these URLs ?
    _urls = []
    _urls.append((r'^' + endPoint, make_auth_verify(authnIndexedEndPointWrapper.verify)))

    authnBroker.add(config.AUTHENTICATION["UserPassword"]["ACR"],  # (?!)
           authnIndexedEndPointWrapper,                      # (?!) method: an identifier of the authentication method.
           config.AUTHENTICATION["UserPassword"]["WEIGHT"],  # security level
           "")                                               # (?!) authentication authority

    # ?!
    authz = AuthzHandling()
    clientDB = shelve_wrapper.open("client_db")
    clientDB = shelve_wrapper.open(config.CLIENTDB)

    provider = Provider(
        config.ISSUER,             # name
        SessionDB(config.ISSUER),  # session database.
        clientDB,                  # client database
        authnBroker,               # authn broker
        None,                      # (?!!) authz  -- Q: are you sure this parameter is set correctly ?
        authz,                     # Client authn -- Q: are you sure this parameter is set correctly ?
        verify_client,             # (?!!) symkey -- Q: are you this parameter is set correctly?
        config.SYM_KEY,            # (?!!) this should be urlmap
        # iv = 0,
        # default_scope = "",
        # ca_bundle = None,
        # verify_ssl = True
        # default_acr = "",
        baseurl=config.ISSUER,
        # server_cls = Server,
        # client_cert = None
        **kwargs)

    # SessionDB:
    # This is database where the provider keeps information about
    # the authenticated/authorised users. It includes information
    # such as "what has been asked for (claims, scopes, and etc. )"
    # and "the state of the session". There is one entry in the
    # database per person
    #
    # __________ Note __________
    # provider.keyjar is an interesting parameter,
    # currently it uses default values, but
    # if you have time, it worth investigating.

    for authnIndexedEndPointWrapper in authnBroker:
        authnIndexedEndPointWrapper.srv = provider

#TODO: this is a point to consider: what if user data in a database?
    if config.USERINFO == "SIMPLE":
        provider.userinfo = UserInfo(config.USERDB)

    provider.cookie_ttl = config.COOKIETTL
    provider.cookie_name = config.COOKIENAME

    if args.debug:
        provider.debug = True

    try:
        # JWK: JSON Web Key
        # JWKS: is a dictionary of JWK
        # __________ NOTE __________
        # JWKS contains private key information.
        #
        # keyjar_init configures cryptographic key
        # based on the provided configuration "keys".
        jwks = keyjar_init(
            provider,                  # server/client instance
            config.keys,          # key configuration
            kid_template="op%d")  # template by which to build the kids (key ID parameter)
    except Exception as err:
        # LOGGER.error("Key setup failed: %s" % err)
        provider.key_setup("static", sig={"format": "jwk", "alg": "rsa"})
    else:
        for key in jwks["keys"]:
            for k in key.keys():
                key[k] = as_unicode(key[k])

        f = open(jwksFileName, "w")
        f.write(json.dumps(jwks))
        f.close()
        provider.jwks_uri = "%s%s" % (provider.baseurl, jwksFileName)

    # for b in OAS.keyjar[""]:
    #    LOGGER.info("OC3 server keys: %s" % b)

    _app = Application(provider, _urls)

    # Setup the web server
    SRV = wsgiserver.CherryPyWSGIServer(('0.0.0.0', config.PORT), _app.application)
    SRV.ssl_adapter = BuiltinSSLAdapter(config.SERVER_CERT, config.SERVER_KEY)

    # LOGGER.info("OC server started (iss={}, port={})".format(config.ISSUER, args.port))

    print "OC server started (iss={}, port={})".format(config.ISSUER, config.PORT)

    try:
        SRV.start()
    except KeyboardInterrupt:
        SRV.stop()
