# Copyright 2024 StackHPC Ltd
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

from neutron.services.trunk.drivers import base as trunk_base
from neutron_lib.api.definitions import port as p_api
from neutron_lib.api.definitions import portbindings
from neutron_lib.api.definitions import trunk as t_api
from neutron_lib.api.definitions import trunk_details as td_api
from neutron_lib.callbacks import events
from neutron_lib.callbacks import registry
from neutron_lib.callbacks import resources
from neutron_lib import constants as n_const
from neutron_lib import context as n_context
from neutron_lib.db import api as db_api
from neutron_lib.plugins import directory
from neutron_lib.services.trunk import constants as t_const
from oslo_config import cfg
from oslo_log import log as logging

LOG = logging.getLogger(__name__)

MECH_DRIVER_NAME = 'genericswitch'

SUPPORTED_INTERFACES = (
    portbindings.VIF_TYPE_OTHER,
)

SUPPORTED_SEGMENTATION_TYPES = (
    t_const.SEGMENTATION_TYPE_VLAN,
)


class GenericSwitchTrunkDriver(trunk_base.DriverBase):

    @property
    def is_loaded(self):
        try:
            return (MECH_DRIVER_NAME in
                    cfg.CONF.ml2.mechanism_drivers)
        except cfg.NoSuchOptError:
            return False

    @registry.receives(resources.TRUNK_PLUGIN, [events.AFTER_INIT])
    def register(self, resource, event, trigger, payload=None):
        super(GenericSwitchTrunkDriver, self).register(
            resource, event, trigger, payload=payload)

        registry.subscribe(self.subport_create,
                           resources.SUBPORTS, events.AFTER_CREATE)
        registry.subscribe(self.subport_delete,
                           resources.SUBPORTS, events.AFTER_DELETE)
        registry.subscribe(self.trunk_create,
                           resources.TRUNK, events.AFTER_CREATE)
        registry.subscribe(self.trunk_update,
                           resources.TRUNK, events.AFTER_UPDATE)
        registry.subscribe(self.trunk_delete,
                           resources.TRUNK, events.AFTER_DELETE)
        self.core_plugin = directory.get_plugin()
        LOG.debug("GenericSwitch trunk driver initialized.")

    @classmethod
    def create(cls, plugin_driver):
        cls.plugin_driver = plugin_driver
        return cls(MECH_DRIVER_NAME,
                   SUPPORTED_INTERFACES,
                   SUPPORTED_SEGMENTATION_TYPES,
                   None,
                   can_trunk_bound_port=True)

    def bind_port(self, parent):
        ctx = n_context.get_admin_context()
        trunk = parent.get(td_api.TRUNK_DETAILS, {})
        subports = trunk.get(t_api.SUB_PORTS, [])
        subport_ids = [subport['port_id'] for subport in subports]
        self._bind_subports(ctx, subport_ids, parent)
        trunk_plugin = directory.get_plugin(t_api.ALIAS)
        trunk_plugin.update_trunk(ctx, trunk.get('trunk_id'),
                                  {t_api.TRUNK:
                                   {'status': t_const.TRUNK_ACTIVE_STATUS}})

    def _bind_subports(self, ctx, subport_ids, parent):
        device_id = parent.get('device_id')
        host_id = parent.get(portbindings.HOST_ID)
        mac_address = parent.get('mac_address')
        port_id = parent.get('id')
        profile = parent.get(portbindings.PROFILE)
        vif_type = parent.get(portbindings.VIF_TYPE)
        vnic_type = parent.get(portbindings.VNIC_TYPE)
        for subport_id in subport_ids:
            context = n_context.get_admin_context()
            with db_api.CONTEXT_READER.using(context):
                self.plugin_driver.subport_create(
                    port_id,
                    subport_id,
                    context)
            self.core_plugin.update_port(
                ctx, subport_id,
                {p_api.RESOURCE_NAME:
                 {portbindings.HOST_ID: host_id,
                  portbindings.VIF_TYPE: vif_type,
                  portbindings.VNIC_TYPE: vnic_type,
                  portbindings.PROFILE: profile,
                  'device_owner': t_const.TRUNK_SUBPORT_OWNER,
                  'device_id': device_id,
                  'mac_address': mac_address,
                  'status': n_const.PORT_STATUS_ACTIVE}})

    def _unbind_subports(self, ctx, subport_ids, parent):
        port_id = parent.get('id')
        for subport_id in subport_ids:
            context = n_context.get_admin_context()
            with db_api.CONTEXT_READER.using(context):
                self.plugin_driver.subport_delete(
                    port_id,
                    subport_id,
                    context)
            self.core_plugin.update_port(
                ctx, subport_id,
                {p_api.RESOURCE_NAME:
                 {portbindings.HOST_ID: None,
                  portbindings.PROFILE: None,
                  'device_owner': '',
                  'device_id': '',
                  'status': n_const.PORT_STATUS_DOWN}})

    def _delete_trunk(self, trunk):
        ctx = n_context.get_admin_context()
        parent_id = trunk.port_id
        parent = self.core_plugin.get_port(ctx, parent_id)
        if parent.get(portbindings.VNIC_TYPE) != portbindings.VNIC_BAREMETAL:
            return
        subport_ids = [subport.port_id
                       for subport in trunk.sub_ports]
        self._unbind_subports(ctx, subport_ids, parent)

    def trunk_create(self, resource, event, trunk_plugin, payload):
        ctx = n_context.get_admin_context()
        parent_id = payload.states[0].port_id
        parent = self.core_plugin.get_port(ctx, parent_id)
        if parent.get(portbindings.VNIC_TYPE) != portbindings.VNIC_BAREMETAL:
            return
        subport_ids = [subport.port_id
                       for subport in payload.states[0].sub_ports]
        self._bind_subports(ctx, subport_ids, parent)
        trunk_plugin.update_trunk(ctx, payload.resource_id,
                                  {t_api.TRUNK:
                                   {'status': parent['status']}})

    def trunk_update(self, resource, event, trunk_plugin, payload):
        if payload.states[1].status != t_const.TRUNK_ACTIVE_STATUS:
            self._delete_trunk(payload.states[1])

    def trunk_delete(self, resource, event, trunk_plugin, payload):
        self._delete_trunk(payload.states[0])

    def subport_create(self, resource, event, trunk_plugin, payload):
        ctx = n_context.get_admin_context()
        parent_id = payload.states[1].port_id
        parent = self.core_plugin.get_port(ctx, parent_id)
        if parent.get(portbindings.VNIC_TYPE) != portbindings.VNIC_BAREMETAL:
            return
        subport_ids = [subport.port_id for subport in
                       payload.metadata['subports']]
        self._bind_subports(ctx, subport_ids, parent)
        trunk_plugin.update_trunk(ctx, payload.resource_id,
                                  {t_api.TRUNK:
                                   {'status': parent['status']}})

    def subport_delete(self, resource, event, trunk_plugin, payload):
        ctx = n_context.get_admin_context()
        parent_id = payload.states[1].port_id
        parent = self.core_plugin.get_port(ctx, parent_id)
        if parent.get(portbindings.VNIC_TYPE) != portbindings.VNIC_BAREMETAL:
            return
        subport_ids = [subport.port_id for subport in
                       payload.metadata['subports']]
        self._unbind_subports(ctx, subport_ids, parent)
        trunk_plugin.update_trunk(ctx, payload.resource_id,
                                  {t_api.TRUNK:
                                   {'status': parent['status']}})
