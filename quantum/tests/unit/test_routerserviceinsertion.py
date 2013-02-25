# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2013 VMware, Inc. All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import unittest2 as unittest
import webob.exc as webexc

import quantum
from quantum.api import extensions
from quantum.api.v2 import router
from quantum.common import config
from quantum.db import db_base_plugin_v2
from quantum.db import l3_db
from quantum.db.loadbalancer import loadbalancer_db as lb_db
from quantum.db import routedserviceinsertion_db as rsi_db
from quantum.db import routerservicetype_db as rst_db
from quantum.db import servicetype_db as st_db
from quantum.extensions import routedserviceinsertion as rsi
from quantum.extensions import routerservicetype as rst
from quantum.openstack.common import cfg
from quantum.plugins.common import constants
from quantum.tests.unit import test_api_v2
from quantum.tests.unit import testlib_api
from quantum import wsgi

_uuid = test_api_v2._uuid
_get_path = test_api_v2._get_path
extensions_path = ':'.join(quantum.extensions.__path__)


class RouterServiceInsertionTestPlugin(
    rst_db.RouterServiceTypeDbMixin,
    rsi_db.RoutedServiceInsertionDbMixin,
    st_db.ServiceTypeManager,
    lb_db.LoadBalancerPluginDb,
    l3_db.L3_NAT_db_mixin,
    db_base_plugin_v2.QuantumDbPluginV2):

    supported_extension_aliases = [
        "router", "router-service-type", "routed-service-insertion",
        "service-type", "lbaas"
    ]

    def create_router(self, context, router):
        with context.session.begin(subtransactions=True):
            r = super(RouterServiceInsertionTestPlugin, self).create_router(
                context, router)
            service_type_id = router['router'].get(rst.SERVICE_TYPE_ID)
            if service_type_id is not None:
                r[rst.SERVICE_TYPE_ID] = service_type_id
                self._process_create_router_service_type_id(
                    context, r)
        return r

    def get_router(self, context, id, fields=None):
        with context.session.begin(subtransactions=True):
            r = super(RouterServiceInsertionTestPlugin, self).get_router(
                context, id, fields)
            rsbind = self._get_router_service_type_id_binding(context, id)
            if rsbind:
                r[rst.SERVICE_TYPE_ID] = rsbind['service_type_id']
        return r

    def delete_router(self, context, id):
        with context.session.begin(subtransactions=True):
            super(RouterServiceInsertionTestPlugin, self).delete_router(
                context, id)
        rsbind = self._get_router_service_type_id_binding(context, id)
        if rsbind:
            raise Exception('Router service-type binding is not deleted')

    def create_resource(self, res, context, resource, model):
        with context.session.begin(subtransactions=True):
            method_name = "create_{0}".format(res)
            method = getattr(super(RouterServiceInsertionTestPlugin, self),
                             method_name)
            o = method(context, resource)
            router_id = resource[res].get(rsi.ROUTER_ID)
            if router_id is not None:
                o[rsi.ROUTER_ID] = router_id
                self._process_create_resource_router_id(
                    context, o, model)
        return o

    def get_resource(self, res, context, id, fields, model):
        method_name = "get_{0}".format(res)
        method = getattr(super(RouterServiceInsertionTestPlugin, self),
                         method_name)
        o = method(context, id, fields)
        if fields is None or rsi.ROUTER_ID in fields:
            rsbind = self._get_resource_router_id_binding(
                context, id, model)
            if rsbind:
                o[rsi.ROUTER_ID] = rsbind['router_id']
        return o

    def delete_resource(self, res, context, id, model):
        method_name = "delete_{0}".format(res)
        with context.session.begin(subtransactions=True):
            method = getattr(super(RouterServiceInsertionTestPlugin, self),
                             method_name)
            method(context, id)
            self._delete_resource_router_id_binding(context, id, model)
        if self._get_resource_router_id_binding(context, id, model):
            raise Exception("{0}-router binding is not deleted".format(res))

    def create_pool(self, context, pool):
        return self.create_resource('pool', context, pool, lb_db.Pool)

    def get_pool(self, context, id, fields=None):
        return self.get_resource('pool', context, id, fields, lb_db.Pool)

    def delete_pool(self, context, id):
        return self.delete_resource('pool', context, id, lb_db.Pool)

    def create_health_monitor(self, context, health_monitor):
        return self.create_resource('health_monitor', context, health_monitor,
                                    lb_db.HealthMonitor)

    def get_health_monitor(self, context, id, fields=None):
        return self.get_resource('health_monitor', context, id, fields,
                                 lb_db.HealthMonitor)

    def delete_health_monitor(self, context, id):
        return self.delete_resource('health_monitor', context, id,
                                    lb_db.HealthMonitor)

    def create_vip(self, context, vip):
        return self.create_resource('vip', context, vip, lb_db.Vip)

    def get_vip(self, context, id, fields=None):
        return self.get_resource(
            'vip', context, id, fields, lb_db.Vip)

    def delete_vip(self, context, id):
        return self.delete_resource('vip', context, id, lb_db.Vip)

    def stats(self, context, pool_id):
        pass


class RouterServiceInsertionTestCase(unittest.TestCase):
    def setUp(self):
        plugin = (
            "quantum.tests.unit.test_routerserviceinsertion."
            "RouterServiceInsertionTestPlugin"
        )

        # point config file to: quantum/tests/etc/quantum.conf.test
        args = ['--config-file', test_api_v2.etcdir('quantum.conf.test')]
        config.parse(args=args)

        #just stubbing core plugin with LoadBalancer plugin
        cfg.CONF.set_override('core_plugin', plugin)
        cfg.CONF.set_override('service_plugins', [plugin])
        cfg.CONF.set_override('quota_router', -1, group='QUOTAS')

        # Ensure 'stale' patched copies of the plugin are never returned
        quantum.manager.QuantumManager._instance = None
        # Ensure existing ExtensionManager is not used

        ext_mgr = extensions.PluginAwareExtensionManager(
            extensions_path,
            {constants.LOADBALANCER: RouterServiceInsertionTestPlugin()}
        )
        extensions.PluginAwareExtensionManager._instance = ext_mgr
        router.APIRouter()

        app = config.load_paste_app('extensions_test_app')
        self._api = extensions.ExtensionMiddleware(app, ext_mgr=ext_mgr)

        self._tenant_id = "8c70909f-b081-452d-872b-df48e6c355d1"

        res = self._do_request('GET', _get_path('service-types'))
        self._service_type_id = res['service_types'][0]['id']

    def tearDown(self):
        self._api = None
        cfg.CONF.reset()

    def _do_request(self, method, path, data=None, params=None, action=None):
        content_type = 'application/json'
        body = None
        if data is not None:  # empty dict is valid
            body = wsgi.Serializer().serialize(data, content_type)

        req = testlib_api.create_request(
            path, body, content_type,
            method, query_string=params)
        res = req.get_response(self._api)
        if res.status_code >= 400:
            raise webexc.HTTPClientError(detail=res.body, code=res.status_code)
        if res.status_code != webexc.HTTPNoContent.code:
            return res.json

    def _router_create(self, service_type_id=None):
        data = {
            "router": {
                "tenant_id": self._tenant_id,
                "name": "test",
                "admin_state_up": True,
                "service_type_id": service_type_id,
            }
        }

        res = self._do_request('POST', _get_path('routers'), data)
        return res['router']

    def test_router_create_no_service_type_id(self):
        router = self._router_create()
        self.assertEqual(router.get('service_type_id'), None)

    def test_router_create_with_service_type_id(self):
        router = self._router_create(self._service_type_id)
        self.assertEqual(router['service_type_id'], self._service_type_id)

    def test_router_get(self):
        router = self._router_create(self._service_type_id)
        res = self._do_request('GET',
                               _get_path('routers/{0}'.format(router['id'])))
        self.assertEqual(res['router']['service_type_id'],
                         self._service_type_id)

    def _test_router_update(self, update_service_type_id):
        router = self._router_create(self._service_type_id)
        router_id = router['id']
        new_name = _uuid()
        data = {
            "router": {
                "name": new_name,
                "admin_state_up": router['admin_state_up'],
            }
        }
        if update_service_type_id:
            data["router"]["service_type_id"] = _uuid()
            with self.assertRaises(webexc.HTTPClientError) as ctx_manager:
                res = self._do_request(
                    'PUT', _get_path('routers/{0}'.format(router_id)), data)
            self.assertEqual(ctx_manager.exception.code, 400)
        else:
            res = self._do_request(
                'PUT', _get_path('routers/{0}'.format(router_id)), data)
            res = self._do_request(
                'GET', _get_path('routers/{0}'.format(router['id'])))
            self.assertEqual(res['router']['name'], new_name)

    def test_router_update_with_service_type_id(self):
        self._test_router_update(True)

    def test_router_update_without_service_type_id(self):
        self._test_router_update(False)

    def test_router_delete(self):
        router = self._router_create(self._service_type_id)
        self._do_request(
            'DELETE', _get_path('routers/{0}'.format(router['id'])))

    def _test_lb_setup(self):
        self._subnet_id = _uuid()
        router = self._router_create(self._service_type_id)
        self._router_id = router['id']

    def _test_pool_setup(self):
        self._test_lb_setup()

    def _test_health_monitor_setup(self):
        self._test_lb_setup()

    def _test_vip_setup(self):
        self._test_pool_setup()
        pool = self._pool_create(self._router_id)
        self._pool_id = pool['id']

    def _create_resource(self, res, data):
        resp = self._do_request('POST', _get_path('lb/{0}s'.format(res)), data)
        return resp[res]

    def _pool_create(self, router_id=None):
        data = {
            "pool": {
                "tenant_id": self._tenant_id,
                "name": "test",
                "protocol": "HTTP",
                "subnet_id": self._subnet_id,
                "lb_method": "ROUND_ROBIN",
                "router_id": router_id
            }
        }

        return self._create_resource('pool', data)

    def _pool_update_attrs(self, pool):
        uattr = {}
        fields = [
            'name', 'description', 'lb_method',
            'health_monitors', 'admin_state_up'
        ]
        for field in fields:
            uattr[field] = pool[field]
        return uattr

    def _health_monitor_create(self, router_id=None):
        data = {
            "health_monitor": {
                "tenant_id": self._tenant_id,
                "type": "HTTP",
                "delay": 1,
                "timeout": 1,
                "max_retries": 1,
                "router_id": router_id
            }
        }

        return self._create_resource('health_monitor', data)

    def _health_monitor_update_attrs(self, hm):
        uattr = {}
        fields = ['delay', 'timeout', 'max_retries']
        for field in fields:
            uattr[field] = hm[field]
        return uattr

    def _vip_create(self, router_id=None):
        data = {
            "vip": {
                "tenant_id": self._tenant_id,
                "name": "test",
                "protocol": "HTTP",
                "port": 80,
                "subnet_id": self._subnet_id,
                "pool_id": self._pool_id,
                "address": "192.168.1.101",
                "connection_limit": 100,
                "admin_state_up": True,
                "router_id": router_id
            }
        }

        return self._create_resource('vip', data)

    def _vip_update_attrs(self, vip):
        uattr = {}
        fields = [
            'name', 'description', 'pool_id', 'connection_limit',
            'admin_state_up'
        ]
        for field in fields:
            uattr[field] = vip[field]
        return uattr

    def _test_resource_create(self, res):
        getattr(self, "_test_{0}_setup".format(res))()
        obj = getattr(self, "_{0}_create".format(res))()
        obj = getattr(self, "_{0}_create".format(res))(self._router_id)
        self.assertEqual(obj['router_id'], self._router_id)

    def _test_resource_update(self, res, update_router_id,
                              update_attr, update_value):
        getattr(self, "_test_{0}_setup".format(res))()
        obj = getattr(self, "_{0}_create".format(res))(self._router_id)
        uattrs = getattr(self, "_{0}_update_attrs".format(res))(obj)
        uattrs[update_attr] = update_value
        data = {res: uattrs}
        if update_router_id:
            uattrs['router_id'] = self._router_id
            with self.assertRaises(webexc.HTTPClientError) as ctx_manager:
                newobj = self._do_request(
                    'PUT',
                    _get_path('lb/{0}s/{1}'.format(res, obj['id'])), data)
            self.assertEqual(ctx_manager.exception.code, 400)
        else:
            newobj = self._do_request(
                'PUT',
                _get_path('lb/{0}s/{1}'.format(res, obj['id'])), data)
            updated = self._do_request(
                'GET',
                _get_path('lb/{0}s/{1}'.format(res, obj['id'])))
            self.assertEqual(updated[res][update_attr], update_value)

    def _test_resource_delete(self, res):
        getattr(self, "_test_{0}_setup".format(res))()
        obj = getattr(self, "_{0}_create".format(res))()
        self._do_request(
            'DELETE', _get_path('lb/{0}s/{1}'.format(res, obj['id'])))
        obj = getattr(self, "_{0}_create".format(res))(self._router_id)
        self._do_request(
            'DELETE', _get_path('lb/{0}s/{1}'.format(res, obj['id'])))

    def test_pool_create(self):
        self._test_resource_create('pool')

    def test_pool_update_with_router_id(self):
        self._test_resource_update('pool', True, 'name', _uuid())

    def test_pool_update_without_router_id(self):
        self._test_resource_update('pool', False, 'name', _uuid())

    def test_pool_delete(self):
        self._test_resource_delete('pool')

    def test_health_monitor_create(self):
        self._test_resource_create('health_monitor')

    def test_health_monitor_update_with_router_id(self):
        self._test_resource_update('health_monitor', True, 'timeout', 2)

    def test_health_monitor_update_without_router_id(self):
        self._test_resource_update('health_monitor', False, 'timeout', 2)

    def test_health_monitor_delete(self):
        self._test_resource_delete('health_monitor')

    def test_vip_create(self):
        self._test_resource_create('vip')

    def test_vip_update_with_router_id(self):
        self._test_resource_update('vip', True, 'name', _uuid())

    def test_vip_update_without_router_id(self):
        self._test_resource_update('vip', False, 'name', _uuid())

    def test_vip_delete(self):
        self._test_resource_delete('vip')
