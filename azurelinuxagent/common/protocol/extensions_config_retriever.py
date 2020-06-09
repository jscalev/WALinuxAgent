# Microsoft Azure Linux Agent
#
# Copyright Microsoft Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Requires Python 2.6+ and Openssl 1.0+
#

import os
import re

from azurelinuxagent.common.future import ustr

import azurelinuxagent.common.conf as conf
import azurelinuxagent.common.logger as logger
from azurelinuxagent.common.protocol.goal_state import ExtensionsConfig
from azurelinuxagent.common.utils import fileutil, restutil
from azurelinuxagent.common.exception import ProtocolError
from azurelinuxagent.common.utils.shellutil import run_command
from azurelinuxagent.common.utils.textutil import safe_shlex_split

INCARNATION_FILE_NAME = "Incarnation"
SEQUENCE_NUMBER_FILE_NAME = "ArtifactProfileSequenceNumber"
SVD_SEQNO_FILE_NAME = "SvdSeqNo"
GOAL_STATE_SOURCE_FILE_NAME = "GoalStateSource"
VM_ID_FILE_NAME = "VmId"

GOAL_STATE_SOURCE_FABRIC = "Fabric"
GOAL_STATE_SOURCE_FASTTRACK = "FastTrack"

DMIDECODE_CALL = "dmidecode"
MSG_PREVIOUSLY_CACHED_PROFILE = "Using previously cached artifacts profile"

EXT_CONF_FILE_NAME = "ExtensionsConfig_{0}.{1}.xml"
EXT_CONFIG_FAST_TRACK = "ft"
EXT_CONFIG_FABRIC = "fa"


class ExtensionsConfigReasons:
    FABRIC_CHANGED = "FabricChanged"
    FABRIC_LAST_CHANGE = "LastFabric"
    FAST_TRACK_CHANGED = "FastTrackChanged"
    FAST_TRACK_LAST_CHANGE = "LastFastTrack"


class FastTrackChangeDetail:
    NO_CHANGE = "NoChange"
    NO_EXTENSIONS = "NoExtensions"
    NO_PROFILE = "NoProfile"
    SEQ_NO_CHANGED = "seqNoChanged"


class FabricChangeDetail:
    INCARNATION_CHANGED = "IncChanged"
    NO_CHANGE = "NoChange"
    NO_INCARNATION = "NoInc"


""" 
GenericExtensionsConfig abstracts whether we pulled the goal state from Fabric or from FastTrack
consumers should not worry from where the ExtensionsConfig came. They should also have no knowledge
of sequence numbers or incarnations, which are specific to FastTrack and Fabric respectfully
"""
class GenericExtensionsConfig(ExtensionsConfig):
    def __init__(self, extensions_config, changed, ext_conf_retriever):
        self.extensions_config = extensions_config
        self.changed = changed
        self._ext_conf_retriever = ext_conf_retriever

        ExtensionsConfig.__init__(self, extensions_config.xml_text)

    def commit_processed(self):
        self._ext_conf_retriever.commit_processed()

    def get_description(self):
        return self._ext_conf_retriever.get_description()

    def get_file_name(self):
        return self._ext_conf_retriever.get_file_name()


class ExtensionsConfigRetriever(object):
    def __init__(self, wire_client):
        self._wire_client = wire_client
        self._last_fabric_incarnation = None
        self._last_svd_seq_no = None
        self._last_fast_track_seq_no = None
        self._saved_artifacts_profile = None
        self._last_mode = None
        self._pending_mode = None
        self._pending_fast_track_seq_no = None
        self._pending_svd_seq_no = None
        self._pending_fabric_incarnation = None
        self._fast_track_changed_detail = None
        self._fabric_changed_detail = None
        self._reason = None
        self._reset_if_necessary()

    def get_ext_config(self, incarnation, ext_conf_uri):
        # If we don't have a uri, return an empty extensions config
        is_startup = False
        if ext_conf_uri is None:
            return GenericExtensionsConfig(ExtensionsConfig(None), False, self)

        # Get the Fabric goal state and whether it changed
        fabric_changed = self._get_fabric_changed(incarnation)

        # Get the VmArtifactsProfile and whether fast track changed, if enabled
        artifacts_profile, fast_track_changed = self._get_fast_track_details()

        self._pending_mode = self._decide_what_to_process(fabric_changed, fast_track_changed)
        if self._last_mode is None:
            is_startup = True
            logger.info("Using {0} for the first call to extensions. Reason={1}", self._pending_mode, self._reason)
        elif self._pending_mode != self._last_mode:
            logger.info("Processing from previous mode {0}. New mode is {1}. Reason={2}",
                        self._last_mode, self._pending_mode, self._reason)

        extensions_config = None
        changed = False
        if self._pending_mode == GOAL_STATE_SOURCE_FABRIC:
            xml_text = self._wire_client.fetch_config(ext_conf_uri, self._wire_client.get_header())
            extensions_config = ExtensionsConfig(xml_text)
            changed = fabric_changed or is_startup
            if fast_track_changed:
                # If FastTrack changed too, then save the artifacts profile because the next time
                # we retrieve it, we'll receive a 304 because the etag didn't change
                self._saved_artifacts_profile = artifacts_profile
        else:
            extensions_config = artifacts_profile.transform_to_extensions_config()
            changed = fast_track_changed or is_startup
            self._saved_artifacts_profile = artifacts_profile

        if changed:
            if self._pending_mode == GOAL_STATE_SOURCE_FABRIC:
                self._remove_extensions_if_necessary(extensions_config)
                self._pending_fabric_incarnation = str(incarnation)
            else:
                self._pending_fast_track_seq_no = artifacts_profile.get_sequence_number()
            logger.info("Handling extension updates. LastMode={0}, IsStartup={1}", self._last_mode, is_startup)

        return GenericExtensionsConfig(extensions_config, changed, self)

    def _get_fast_track_details(self):
        artifacts_profile = None
        fast_track_changed = False
        
        if conf.get_extensions_fast_track_enabled():
            artifacts_profile = self._wire_client.get_artifacts_profile()
            if artifacts_profile is None and self._saved_artifacts_profile is not None:
                logger.periodic_info(logger.EVERY_DAY, MSG_PREVIOUSLY_CACHED_PROFILE)
                artifacts_profile = self._saved_artifacts_profile
            else:
                # If we use the cached profile again, we want to see that message
                logger.reset_periodic_msg(MSG_PREVIOUSLY_CACHED_PROFILE)
            fast_track_changed = self._get_fast_track_changed(artifacts_profile)
        return artifacts_profile, fast_track_changed

    def commit_processed(self):
        if self._last_mode is None:
            logger.info("Finish and save data for first mode {0}.", self._pending_mode)
        elif self._pending_mode != self._last_mode:
            logger.info("Finish and save data from previous mode {0}. New mode is {1}", self._last_mode, self._pending_mode)

        if self._pending_mode == GOAL_STATE_SOURCE_FASTTRACK:
            self._last_fast_track_seq_no = self._pending_fast_track_seq_no
            self._set_fast_track(self._last_fast_track_seq_no)
            self._last_mode = self._pending_mode
        else:
            self._last_fabric_incarnation = self._pending_fabric_incarnation

            # Don't record the last goal state as Fabric if we didn't process the extensions
            if self._last_svd_seq_no != self._pending_svd_seq_no:
                self._last_svd_seq_no = self._pending_svd_seq_no
                self._last_mode = self._pending_mode
                self._set_fabric(self._last_fabric_incarnation, self._last_svd_seq_no)

    def _reset(self):
        """
        Removes all cache files and resets all cached goal state information
        This is necessary if a VM image is deployed from this one so we start fresh
        """
        self._remove_cache(INCARNATION_FILE_NAME)
        self._remove_cache(SEQUENCE_NUMBER_FILE_NAME)
        self._remove_cache(SVD_SEQNO_FILE_NAME)
        self._remove_cache(GOAL_STATE_SOURCE_FILE_NAME)
        self._remove_cache(VM_ID_FILE_NAME)

    def _reset_if_necessary(self):
        cached_vm_id = self._get_saved_vm_id()
        current_vm_id = self._get_vm_id()
        if current_vm_id is None:
            logger.warn("Unable to retrieve the current vm id. Skipping reset")
        elif cached_vm_id is None:
            logger.info("Remembering current vm id is {0}".format(current_vm_id))
            self._set_cached_vm_id(current_vm_id)
        elif current_vm_id != cached_vm_id:
            logger.warn("The vm id has changed from {0} to {1}. Resetting cached state".format(cached_vm_id, current_vm_id))
            self._reset()
            self._set_cached_vm_id(current_vm_id)

    def _remove_extensions_if_necessary(self, extensions_config):
        """
        If this is a Fabric GS, but the InSvdSeqNo did NOT change, then the goal state was
        created directly by Fabric and bypassed CRP. A common scenario is remote access.
        Another is when wire server restarts and uses a new incarnation.
        The problem is any extensions contained here may be out of date, because they were
        more recently updated via a FastTrack GS. Therefore, we remove them here in that case.
        """
        if self._pending_mode == GOAL_STATE_SOURCE_FABRIC:
            svd_seq_no = self._last_svd_seq_no
            if svd_seq_no is None:
                svd_seq_no = self._get_saved_svd_seqNo()
            if str(extensions_config.svd_seqNo) == str(svd_seq_no):
                logger.info("SvdSeqNo did not change. Removing extensions from goal state")
                extensions_config.ext_handlers = None
                self._last_svd_seq_no = extensions_config.svd_seqNo
                self._pending_svd_seq_no = extensions_config.svd_seqNo
            else:
                self._pending_svd_seq_no = svd_seq_no

    def _decide_what_to_process(self, fabric_changed, fast_track_changed):
        """
        If just Fabric GS changed, then process only that.
        If just FastTrack GS changed, then process only that.
        If both changed, then process Fabric and then FastTrack.
        If neither changed, then process whichever we used last (to keep with the current behavior)
        """
        if fabric_changed:
            self._set_reason(ExtensionsConfigReasons.FABRIC_CHANGED)
            return GOAL_STATE_SOURCE_FABRIC
        if fast_track_changed:
            self._set_reason(ExtensionsConfigReasons.FAST_TRACK_CHANGED)
            return GOAL_STATE_SOURCE_FASTTRACK

        mode = self._last_mode
        if mode is None:
            mode = self._get_saved_mode()
        if mode == GOAL_STATE_SOURCE_FASTTRACK:
            self._set_reason(ExtensionsConfigReasons.FAST_TRACK_LAST_CHANGE)
            return GOAL_STATE_SOURCE_FASTTRACK

        self._set_reason(ExtensionsConfigReasons.FABRIC_LAST_CHANGE)
        return GOAL_STATE_SOURCE_FABRIC

    def _set_reason(self, reason):
        self._reason = "{0} FastTrack={1}, Fabric={2}".format(reason, self._fast_track_changed_detail, self._fabric_changed_detail)

    def _get_fast_track_changed(self, artifacts_profile):
        if artifacts_profile is None:
            self._fast_track_changed_detail = FastTrackChangeDetail.NO_PROFILE
            return False
        if not artifacts_profile.has_extensions():
            self._fast_track_changed_detail = FastTrackChangeDetail.NO_EXTENSIONS
            return False

        sequence_number = self._get_last_sequence_number()
        if sequence_number is None or sequence_number != artifacts_profile.get_sequence_number():
            self._fast_track_changed_detail = FastTrackChangeDetail.SEQ_NO_CHANGED
            return True

        self._fast_track_changed_detail = FastTrackChangeDetail.NO_CHANGE
        return False

    def _get_fabric_changed(self, goal_state_incarnation):
        if goal_state_incarnation is None:
            self._fabric_changed_detail = FabricChangeDetail.NO_INCARNATION
            return True

        incarnation = self._get_last_incarnation()
        if incarnation is None or str(incarnation) != str(goal_state_incarnation):
            self._fabric_changed_detail = FabricChangeDetail.INCARNATION_CHANGED
            return True

        self._fabric_changed_detail = FabricChangeDetail.NO_CHANGE
        return False

    def _get_last_sequence_number(self):
        sequence_number = self._last_fast_track_seq_no
        if sequence_number is None:
            sequence_number = self._get_saved_sequence_number()
        return sequence_number

    def _get_last_incarnation(self):
        incarnation = self._last_fabric_incarnation
        if incarnation is None:
            incarnation = self._get_saved_incarnation()
        return incarnation

    def _set_fast_track(self, vm_artifacts_seq_no=None):
        path = os.path.join(conf.get_lib_dir(), GOAL_STATE_SOURCE_FILE_NAME)
        self._save_cache(path, GOAL_STATE_SOURCE_FASTTRACK)
        if vm_artifacts_seq_no is not None:
            sequence_number_file_path = os.path.join(conf.get_lib_dir(), SEQUENCE_NUMBER_FILE_NAME)
            self._save_cache(sequence_number_file_path, ustr(vm_artifacts_seq_no))

    def _set_fabric(self, incarnation=None, svd_seqNo = None):
        path = os.path.join(conf.get_lib_dir(), GOAL_STATE_SOURCE_FILE_NAME)
        self._save_cache(path, GOAL_STATE_SOURCE_FABRIC)
        if incarnation is not None:
            incarnation_file_path = os.path.join(conf.get_lib_dir(), INCARNATION_FILE_NAME)
            self._save_cache(incarnation_file_path, ustr(incarnation))
        if svd_seqNo is not None:
            svd_seqNo_file_path = os.path.join(conf.get_lib_dir(), SVD_SEQNO_FILE_NAME)
            self._save_cache(svd_seqNo_file_path, ustr(svd_seqNo))

    def _remove_cache(self, file_name):
        try:
            path = os.path.join(conf.get_lib_dir(), file_name)
            if os.path.exists(path):
                os.remove(path)
        except IOError as e:
            fileutil.clean_ioerror(e, paths=path)
            raise ProtocolError("Failed to remove cache: {0}".format(e))

    def _set_cached_vm_id(self, cached_vm_id):
        path = os.path.join(conf.get_lib_dir(), VM_ID_FILE_NAME)
        self._save_cache(path, cached_vm_id)

    def _save_cache(self, local_file, data):
        try:
            fileutil.write_file(local_file, data)
        except IOError as e:
            fileutil.clean_ioerror(e, paths=[local_file])
            raise ProtocolError("Failed to write cache: {0}".format(e))

    def _get_saved_sequence_number(self):
        path = os.path.join(conf.get_lib_dir(), SEQUENCE_NUMBER_FILE_NAME)
        if os.path.exists(path):
            sequence_number = fileutil.read_file(path)
            if sequence_number is not None:
                return int(sequence_number)
        return -1

    def _get_saved_svd_seqNo(self):
        path = os.path.join(conf.get_lib_dir(), SVD_SEQNO_FILE_NAME)
        if os.path.exists(path):
            svd_seqno = fileutil.read_file(path)
            if svd_seqno is not None:
                return int(svd_seqno)
        return -1

    def _get_saved_incarnation(self):
        path = os.path.join(conf.get_lib_dir(), INCARNATION_FILE_NAME)
        if os.path.exists(path):
            incarnation = fileutil.read_file(path)
            if incarnation is not None:
                return str(incarnation)
        return -1

    def _get_saved_mode(self):
        path = os.path.join(conf.get_lib_dir(), GOAL_STATE_SOURCE_FILE_NAME)
        if os.path.exists(path):
            goal_state_source = fileutil.read_file(path)
            return goal_state_source
        else:
            return GOAL_STATE_SOURCE_FABRIC

    def _get_saved_vm_id(self):
        cached_vm_id = None
        path = os.path.join(conf.get_lib_dir(), VM_ID_FILE_NAME)
        if os.path.exists(path):
            cached_vm_id = fileutil.read_file(path)
        return cached_vm_id

    def get_description(self):
        return "{0} Incarnation={1} SeqNo={2} Reason={3}".format(
            self._last_mode, self._last_fabric_incarnation, self._last_fast_track_seq_no, self._reason)

    def get_file_name(self):
        if self._last_mode == GOAL_STATE_SOURCE_FASTTRACK:
            return EXT_CONF_FILE_NAME.format(EXT_CONFIG_FAST_TRACK, self._get_last_sequence_number())
        else:
            return EXT_CONF_FILE_NAME.format(EXT_CONFIG_FABRIC, self._get_last_incarnation())

    def _get_vm_id(self):
        vm_id = None
        try:
            tokenized = safe_shlex_split(DMIDECODE_CALL)
            result = run_command(tokenized, log_error=True)
            uuid_pos = result.find("UUID:")
            uuid_len = len("UUID: ")
            new_line_pos = result.find('\n', uuid_pos)
            vm_id = result[uuid_pos + uuid_len : new_line_pos]
        except Exception as e:
            logger.warn("Unable to retrieve VmId: {0}".format(e))
        return vm_id
