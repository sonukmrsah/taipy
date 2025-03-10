# Copyright 2023 Avaiga Private Limited
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with
# the License. You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
# an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.

import json
import typing as t
from collections import defaultdict
from numbers import Number
from threading import Lock

try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo  # type: ignore[no-redef]

import pandas as pd
from dateutil import parser

from taipy.config import Config
from taipy.core import (
    Cycle,
    DataNode,
    DataNodeId,
    Job,
    JobId,
    Scenario,
    ScenarioId,
    Sequence,
    SequenceId,
    cancel_job,
    create_scenario,
)
from taipy.core import delete as core_delete
from taipy.core import delete_job
from taipy.core import get as core_get
from taipy.core import (
    get_cycles_scenarios,
    get_data_nodes,
    get_jobs,
    is_deletable,
    is_editable,
    is_promotable,
    is_readable,
    is_submittable,
    set_primary,
)
from taipy.core import submit as core_submit
from taipy.core.data._abstract_tabular import _AbstractTabularDataNode
from taipy.core.notification import CoreEventConsumerBase, EventEntityType
from taipy.core.notification.event import Event, EventOperation
from taipy.core.notification.notifier import Notifier
from taipy.core.submission.submission import Submission
from taipy.core.submission._submission_manager_factory import _SubmissionManagerFactory
from taipy.core.submission.submission_status import SubmissionStatus
from taipy.gui import Gui, State
from taipy.gui._warnings import _warn
from taipy.gui.gui import _DoNotUpdate

from ._adapters import _EntityType


class _SubmissionDetails:
    def __init__(
        self,
        client_id: str,
        module_context: str,
        callback: t.Callable,
        submission: Submission,
    ) -> None:
        self.client_id = client_id
        self.module_context = module_context
        self.callback = callback
        self.submission = submission

    def set_submission(self, submission: Submission):
        self.submission = submission
        return self


class _GuiCoreContext(CoreEventConsumerBase):
    __PROP_ENTITY_ID = "id"
    __PROP_ENTITY_COMMENT = "comment"
    __PROP_CONFIG_ID = "config"
    __PROP_DATE = "date"
    __PROP_ENTITY_NAME = "name"
    __PROP_SCENARIO_PRIMARY = "primary"
    __PROP_SCENARIO_TAGS = "tags"
    __ENTITY_PROPS = (__PROP_CONFIG_ID, __PROP_DATE, __PROP_ENTITY_NAME)
    __ACTION = "action"
    _CORE_CHANGED_NAME = "core_changed"
    _SCENARIO_SELECTOR_ERROR_VAR = "gui_core_sc_error"
    _SCENARIO_SELECTOR_ID_VAR = "gui_core_sc_id"
    _SCENARIO_VIZ_ERROR_VAR = "gui_core_sv_error"
    _JOB_SELECTOR_ERROR_VAR = "gui_core_js_error"
    _DATANODE_VIZ_ERROR_VAR = "gui_core_dv_error"
    _DATANODE_VIZ_OWNER_ID_VAR = "gui_core_dv_owner_id"
    _DATANODE_VIZ_HISTORY_ID_VAR = "gui_core_dv_history_id"
    _DATANODE_VIZ_DATA_ID_VAR = "gui_core_dv_data_id"
    _DATANODE_VIZ_DATA_CHART_ID_VAR = "gui_core_dv_data_chart_id"
    _DATANODE_VIZ_DATA_NODE_PROP = "data_node"

    def __init__(self, gui: Gui) -> None:
        self.gui = gui
        self.scenario_by_cycle: t.Optional[t.Dict[t.Optional[Cycle], t.List[Scenario]]] = None
        self.data_nodes_by_owner: t.Optional[t.Dict[t.Optional[str], DataNode]] = None
        self.scenario_configs: t.Optional[t.List[t.Tuple[str, str]]] = None
        self.jobs_list: t.Optional[t.List[Job]] = None
        self.client_submission: t.Dict[str, _SubmissionDetails] = dict()
        # register to taipy core notification
        reg_id, reg_queue = Notifier.register()
        # locks
        self.lock = Lock()
        self.submissions_lock = Lock()
        # super
        super().__init__(reg_id, reg_queue)
        self.start()

    def process_event(self, event: Event):
        if event.entity_type == EventEntityType.SCENARIO:
            if event.operation == EventOperation.SUBMISSION:
                self.scenario_status_callback(event.attribute_name)
                return
            self.scenario_refresh(
                event.entity_id
                if event.operation != EventOperation.DELETION and is_readable(t.cast(ScenarioId, event.entity_id))
                else None
            )
        elif event.entity_type == EventEntityType.SEQUENCE and event.entity_id:
            sequence = None
            try:
                sequence = (
                    core_get(event.entity_id)
                    if event.operation != EventOperation.DELETION and is_readable(t.cast(SequenceId, event.entity_id))
                    else None
                )
                if sequence and hasattr(sequence, "parent_ids") and sequence.parent_ids:
                    self.gui._broadcast(
                        _GuiCoreContext._CORE_CHANGED_NAME, {"scenario": [x for x in sequence.parent_ids]}
                    )
            except Exception as e:
                _warn(f"Access to sequence {event.entity_id} failed", e)
        elif event.entity_type == EventEntityType.JOB:
            with self.lock:
                self.jobs_list = None
        elif event.entity_type == EventEntityType.SUBMISSION:
            self.scenario_status_callback(event.entity_id)
        elif event.entity_type == EventEntityType.DATA_NODE:
            with self.lock:
                self.data_nodes_by_owner = None
            self.gui._broadcast(
                _GuiCoreContext._CORE_CHANGED_NAME,
                {"datanode": event.entity_id if event.operation != EventOperation.DELETION else True},
            )

    def scenario_refresh(self, scenario_id: t.Optional[str]):
        with self.lock:
            self.scenario_by_cycle = None
            self.data_nodes_by_owner = None
        self.gui._broadcast(
            _GuiCoreContext._CORE_CHANGED_NAME,
            {"scenario": scenario_id or True},
        )

    def scenario_status_callback(self, submission_id: t.Optional[str]):
        if not submission_id or not is_readable_submission(submission_id):
            return
        try:
            sub_details: t.Optional[_SubmissionDetails] = self.client_submission.get(submission_id)
            if not sub_details:
                return

            submission = core_get_submission(submission_id)
            if not submission or not submission.entity_id:
                return

            entity = core_get(submission.entity_id)
            if not entity:
                return

            new_status = submission.submission_status
            if sub_details.submission.submission_status != new_status:
                # callback
                self.gui._call_user_callback(
                    sub_details.client_id,
                    sub_details.callback,
                    [entity, {"submission_status": new_status.name}],
                    sub_details.module_context,
                )
            with self.submissions_lock:
                if new_status in (
                    SubmissionStatus.COMPLETED,
                    SubmissionStatus.FAILED,
                    SubmissionStatus.CANCELED,
                ):
                    self.client_submission.pop(submission_id, None)
                else:
                    self.client_submission[submission_id] = sub_details.set_submission(submission)

        except Exception as e:
            _warn(f"Submission ({submission_id}) is not available", e)

        finally:
            self.gui._broadcast(_GuiCoreContext._CORE_CHANGED_NAME, {"jobs": True})

    def scenario_adapter(self, scenario_or_cycle):
        try:
            if (
                hasattr(scenario_or_cycle, "id")
                and is_readable(scenario_or_cycle.id)
                and core_get(scenario_or_cycle.id) is not None
            ):
                if self.scenario_by_cycle and isinstance(scenario_or_cycle, Cycle):
                    return (
                        scenario_or_cycle.id,
                        scenario_or_cycle.get_simple_label(),
                        self.scenario_by_cycle.get(scenario_or_cycle),
                        _EntityType.CYCLE.value,
                        False,
                    )
                elif isinstance(scenario_or_cycle, Scenario):
                    return (
                        scenario_or_cycle.id,
                        scenario_or_cycle.get_simple_label(),
                        None,
                        _EntityType.SCENARIO.value,
                        scenario_or_cycle.is_primary,
                    )
        except Exception as e:
            _warn(
                f"Access to {type(scenario_or_cycle)} "
                + f"({scenario_or_cycle.id if hasattr(scenario_or_cycle, 'id') else 'No_id'})"
                + " failed",
                e,
            )
        return None

    def get_scenarios(self):
        cycles_scenarios = []
        with self.lock:
            if self.scenario_by_cycle is None:
                self.scenario_by_cycle = get_cycles_scenarios()
            for cycle, scenarios in self.scenario_by_cycle.items():
                if cycle is None:
                    cycles_scenarios.extend(scenarios)
                else:
                    cycles_scenarios.append(cycle)
        return cycles_scenarios

    def select_scenario(self, state: State, id: str, payload: t.Dict[str, str]):
        args = payload.get("args")
        if args is None or not isinstance(args, list) or len(args) == 0:
            return
        state.assign(_GuiCoreContext._SCENARIO_SELECTOR_ID_VAR, args[0])

    def get_scenario_by_id(self, id: str) -> t.Optional[Scenario]:
        if not id or not is_readable(t.cast(ScenarioId, id)):
            return None
        try:
            return core_get(t.cast(ScenarioId, id))
        except Exception:
            return None

    def get_scenario_configs(self):
        with self.lock:
            if self.scenario_configs is None:
                configs = Config.scenarios
                if isinstance(configs, dict):
                    self.scenario_configs = [(id, f"{c.id}") for id, c in configs.items() if id != "default"]
            return self.scenario_configs

    def crud_scenario(self, state: State, id: str, payload: t.Dict[str, str]):  # noqa: C901
        args = payload.get("args")
        if (
            args is None
            or not isinstance(args, list)
            or len(args) < 3
            or not isinstance(args[0], bool)
            or not isinstance(args[1], bool)
            or not isinstance(args[2], dict)
        ):
            return
        update = args[0]
        delete = args[1]
        data = args[2]
        scenario = None

        name = data.get(_GuiCoreContext.__PROP_ENTITY_NAME)
        if update:
            scenario_id = data.get(_GuiCoreContext.__PROP_ENTITY_ID)
            if delete:
                if not is_deletable(scenario_id):
                    state.assign(
                        _GuiCoreContext._SCENARIO_SELECTOR_ERROR_VAR, f"Scenario. {scenario_id} is not deletable."
                    )
                    return
                try:
                    core_delete(scenario_id)
                except Exception as e:
                    state.assign(_GuiCoreContext._SCENARIO_SELECTOR_ERROR_VAR, f"Error deleting Scenario. {e}")
            else:
                if not self.__check_readable_editable(
                    state, scenario_id, "Scenario", _GuiCoreContext._SCENARIO_SELECTOR_ERROR_VAR
                ):
                    return
                scenario = core_get(scenario_id)
        else:
            config_id = data.get(_GuiCoreContext.__PROP_CONFIG_ID)
            scenario_config = Config.scenarios.get(config_id)
            if scenario_config is None:
                state.assign(_GuiCoreContext._SCENARIO_SELECTOR_ERROR_VAR, f"Invalid configuration id ({config_id})")
                return
            date_str = data.get(_GuiCoreContext.__PROP_DATE)
            try:
                date = parser.parse(date_str) if isinstance(date_str, str) else None
            except Exception as e:
                state.assign(_GuiCoreContext._SCENARIO_SELECTOR_ERROR_VAR, f"Invalid date ({date_str}).{e}")
                return
            scenario_id = None
            try:
                gui: Gui = state._gui
                on_creation = args[3] if len(args) > 3 and isinstance(args[3], str) else None
                on_creation_function = gui._get_user_function(on_creation) if on_creation else None
                if callable(on_creation_function):
                    try:
                        res = gui._call_function_with_state(
                            on_creation_function,
                            [
                                id,
                                {
                                    "action": on_creation,
                                    "config": scenario_config,
                                    "date": date,
                                    "label": name,
                                    "properties": {
                                        v.get("key"): v.get("value") for v in data.get("properties", dict())
                                    },
                                },
                            ],
                        )
                        if isinstance(res, Scenario):
                            # everything's fine
                            scenario_id = res.id
                            state.assign(_GuiCoreContext._SCENARIO_SELECTOR_ERROR_VAR, "")
                            return
                        if res:
                            # do not create
                            state.assign(_GuiCoreContext._SCENARIO_SELECTOR_ERROR_VAR, f"{res}")
                            return
                    except Exception as e:  # pragma: no cover
                        if not gui._call_on_exception(on_creation, e):
                            _warn(f"on_creation(): Exception raised in '{on_creation}()'", e)
                        state.assign(
                            _GuiCoreContext._SCENARIO_SELECTOR_ERROR_VAR,
                            f"Error creating Scenario with '{on_creation}()'. {e}",
                        )
                        return
                elif on_creation is not None:
                    _warn(f"on_creation(): '{on_creation}' is not a function.")
                scenario = create_scenario(scenario_config, date, name)
                scenario_id = scenario.id
            except Exception as e:
                state.assign(_GuiCoreContext._SCENARIO_SELECTOR_ERROR_VAR, f"Error creating Scenario. {e}")
            finally:
                self.scenario_refresh(scenario_id)
        if scenario:
            if not is_editable(scenario):
                state.assign(
                    _GuiCoreContext._SCENARIO_SELECTOR_ERROR_VAR, f"Scenario {scenario_id or name} is not editable."
                )
                return
            with scenario as sc:
                sc.properties[_GuiCoreContext.__PROP_ENTITY_NAME] = name
                if props := data.get("properties"):
                    try:
                        new_keys = [prop.get("key") for prop in props]
                        for key in t.cast(dict, sc.properties).keys():
                            if key and key not in _GuiCoreContext.__ENTITY_PROPS and key not in new_keys:
                                t.cast(dict, sc.properties).pop(key, None)
                        for prop in props:
                            key = prop.get("key")
                            if key and key not in _GuiCoreContext.__ENTITY_PROPS:
                                sc._properties[key] = prop.get("value")
                        state.assign(_GuiCoreContext._SCENARIO_SELECTOR_ERROR_VAR, "")
                    except Exception as e:
                        state.assign(_GuiCoreContext._SCENARIO_SELECTOR_ERROR_VAR, f"Error creating Scenario. {e}")

    def edit_entity(self, state: State, id: str, payload: t.Dict[str, str]):
        args = payload.get("args")
        if args is None or not isinstance(args, list) or len(args) < 1 or not isinstance(args[0], dict):
            return
        data = args[0]
        entity_id = data.get(_GuiCoreContext.__PROP_ENTITY_ID)
        if not self.__check_readable_editable(
            state, entity_id, data.get("type", "Scenario"), _GuiCoreContext._SCENARIO_VIZ_ERROR_VAR
        ):
            return
        entity: t.Union[Scenario, Sequence] = core_get(entity_id)
        if entity:
            try:
                if isinstance(entity, Scenario):
                    primary = data.get(_GuiCoreContext.__PROP_SCENARIO_PRIMARY)
                    if primary is True:
                        if not is_promotable(entity):
                            state.assign(
                                _GuiCoreContext._SCENARIO_VIZ_ERROR_VAR, f"Scenario {entity_id} is not promotable."
                            )
                            return
                        set_primary(entity)
                self.__edit_properties(entity, data)
                state.assign(_GuiCoreContext._SCENARIO_VIZ_ERROR_VAR, "")
            except Exception as e:
                state.assign(_GuiCoreContext._SCENARIO_VIZ_ERROR_VAR, f"Error updating Scenario. {e}")

    def submit_entity(self, state: State, id: str, payload: t.Dict[str, str]):
        args = payload.get("args")
        if args is None or not isinstance(args, list) or len(args) < 1 or not isinstance(args[0], dict):
            return
        data = args[0]
        entity_id = data.get(_GuiCoreContext.__PROP_ENTITY_ID)
        if not is_submittable(entity_id):
            state.assign(
                _GuiCoreContext._SCENARIO_VIZ_ERROR_VAR,
                f"{data.get('type', 'Scenario')} {entity_id} is not submittable.",
            )
            return
        entity = core_get(entity_id)
        if entity:
            try:
                jobs = core_submit(entity)
                submission_entity = core_get_submission(jobs[0].submit_id if isinstance(jobs, list) else jobs.submit_id)
                if submission_cb := data.get("on_submission_change"):
                    submission_fn = self.gui._get_user_function(submission_cb)
                    if callable(submission_fn):
                        client_id = self.gui._get_client_id()
                        module_context = self.gui._get_locals_context()
                        with self.submissions_lock:
                            self.client_submission[submission_entity.id] = _SubmissionDetails(
                                client_id,
                                module_context,
                                submission_fn,
                                submission_entity,
                            )
                    else:
                        _warn(f"on_submission_change(): '{submission_cb}' is not a valid function.")
                self.scenario_status_callback(submission_entity.id)
                state.assign(_GuiCoreContext._SCENARIO_VIZ_ERROR_VAR, "")
            except Exception as e:
                state.assign(_GuiCoreContext._SCENARIO_VIZ_ERROR_VAR, f"Error submitting entity. {e}")

    def __do_datanodes_tree(self):
        if self.data_nodes_by_owner is None:
            self.data_nodes_by_owner = defaultdict(list)
            for dn in get_data_nodes():
                self.data_nodes_by_owner[dn.owner_id].append(dn)

    def get_datanodes_tree(self):
        with self.lock:
            self.__do_datanodes_tree()
        return self.data_nodes_by_owner.get(None, []) + self.get_scenarios()

    def data_node_adapter(self, data):
        try:
            if hasattr(data, "id") and is_readable(data.id) and core_get(data.id) is not None:
                if isinstance(data, DataNode):
                    return (data.id, data.get_simple_label(), None, _EntityType.DATANODE.value, False)
                else:
                    with self.lock:
                        self.__do_datanodes_tree()
                        if self.data_nodes_by_owner:
                            if isinstance(data, Cycle):
                                return (
                                    data.id,
                                    data.get_simple_label(),
                                    self.data_nodes_by_owner[data.id] + self.scenario_by_cycle.get(data, []),
                                    _EntityType.CYCLE.value,
                                    False,
                                )
                            elif isinstance(data, Scenario):
                                return (
                                    data.id,
                                    data.get_simple_label(),
                                    self.data_nodes_by_owner[data.id] + list(data.sequences.values()),
                                    _EntityType.SCENARIO.value,
                                    data.is_primary,
                                )
                            elif isinstance(data, Sequence):
                                if datanodes := self.data_nodes_by_owner.get(data.id):
                                    return (
                                        data.id,
                                        data.get_simple_label(),
                                        datanodes,
                                        _EntityType.SEQUENCE.value,
                                        False,
                                    )
        except Exception as e:
            _warn(
                f"Access to {type(data)} ({data.id if hasattr(data, 'id') else 'No_id'}) failed",
                e,
            )

        return None

    def get_jobs_list(self):
        with self.lock:
            if self.jobs_list is None:
                self.jobs_list = get_jobs()
            return self.jobs_list

    def job_adapter(self, job):
        try:
            if hasattr(job, "id") and is_readable(job.id) and core_get(job.id) is not None:
                if isinstance(job, Job):
                    entity = core_get(job.owner_id)
                    return (
                        job.id,
                        job.get_simple_label(),
                        [],
                        entity.get_simple_label() if entity else "",
                        entity.id if entity else "",
                        job.submit_id,
                        job.creation_date,
                        job.status.value,
                        is_deletable(job),
                        is_readable(job),
                        is_editable(job),
                    )
        except Exception as e:
            _warn(f"Access to job ({job.id if hasattr(job, 'id') else 'No_id'}) failed", e)
        return None

    def act_on_jobs(self, state: State, id: str, payload: t.Dict[str, str]):
        args = payload.get("args")
        if args is None or not isinstance(args, list) or len(args) < 1 or not isinstance(args[0], dict):
            return
        data = args[0]
        job_ids = data.get(_GuiCoreContext.__PROP_ENTITY_ID)
        job_action = data.get(_GuiCoreContext.__ACTION)
        if job_action and isinstance(job_ids, list):
            errs = []
            if job_action == "delete":
                for job_id in job_ids:
                    if not is_readable(job_id):
                        errs.append(f"Job {job_id} is not readable.")
                        continue
                    if not is_deletable(job_id):
                        errs.append(f"Job {job_id} is not deletable.")
                        continue
                    try:
                        delete_job(core_get(job_id))
                    except Exception as e:
                        errs.append(f"Error deleting job. {e}")
            elif job_action == "cancel":
                for job_id in job_ids:
                    if not is_readable(job_id):
                        errs.append(f"Job {job_id} is not readable.")
                        continue
                    if not is_editable(job_id):
                        errs.append(f"Job {job_id} is not cancelable.")
                        continue
                    try:
                        cancel_job(job_id)
                    except Exception as e:
                        errs.append(f"Error canceling job. {e}")
            state.assign(_GuiCoreContext._JOB_SELECTOR_ERROR_VAR, "<br/>".join(errs) if errs else "")

    def edit_data_node(self, state: State, id: str, payload: t.Dict[str, str]):
        args = payload.get("args")
        if args is None or not isinstance(args, list) or len(args) < 1 or not isinstance(args[0], dict):
            return
        data = args[0]
        entity_id = data.get(_GuiCoreContext.__PROP_ENTITY_ID)
        if not self.__check_readable_editable(state, entity_id, "DataNode", _GuiCoreContext._DATANODE_VIZ_ERROR_VAR):
            return
        entity: DataNode = core_get(entity_id)
        if isinstance(entity, DataNode):
            try:
                self.__edit_properties(entity, data)
                state.assign(_GuiCoreContext._DATANODE_VIZ_ERROR_VAR, "")
            except Exception as e:
                state.assign(_GuiCoreContext._DATANODE_VIZ_ERROR_VAR, f"Error updating Datanode. {e}")

    def lock_datanode_for_edit(self, state: State, id: str, payload: t.Dict[str, str]):
        args = payload.get("args")
        if args is None or not isinstance(args, list) or len(args) < 1 or not isinstance(args[0], dict):
            return
        data = args[0]
        entity_id = data.get(_GuiCoreContext.__PROP_ENTITY_ID)
        if not self.__check_readable_editable(state, entity_id, "Datanode", _GuiCoreContext._DATANODE_VIZ_ERROR_VAR):
            return
        lock = data.get("lock", True)
        entity: DataNode = core_get(entity_id)
        if isinstance(entity, DataNode):
            try:
                if lock:
                    entity.lock_edit(self.gui._get_client_id())
                else:
                    entity.unlock_edit(self.gui._get_client_id())
                state.assign(_GuiCoreContext._DATANODE_VIZ_ERROR_VAR, "")
            except Exception as e:
                state.assign(_GuiCoreContext._DATANODE_VIZ_ERROR_VAR, f"Error locking Datanode. {e}")

    def __edit_properties(self, entity: t.Union[Scenario, Sequence, DataNode], data: t.Dict[str, str]):
        with entity as ent:
            if isinstance(ent, Scenario):
                tags = data.get(_GuiCoreContext.__PROP_SCENARIO_TAGS)
                if isinstance(tags, (list, tuple)):
                    ent.tags = {t for t in tags}
            name = data.get(_GuiCoreContext.__PROP_ENTITY_NAME)
            if isinstance(name, str):
                ent.properties[_GuiCoreContext.__PROP_ENTITY_NAME] = name
            props = data.get("properties")
            if isinstance(props, (list, tuple)):
                for prop in props:
                    key = prop.get("key")
                    if key and key not in _GuiCoreContext.__ENTITY_PROPS:
                        ent.properties[key] = prop.get("value")
            deleted_props = data.get("deleted_properties")
            if isinstance(deleted_props, (list, tuple)):
                for prop in deleted_props:
                    key = prop.get("key")
                    if key and key not in _GuiCoreContext.__ENTITY_PROPS:
                        ent.properties.pop(key, None)

    def get_scenarios_for_owner(self, owner_id: str):
        cycles_scenarios: t.List[t.Union[Scenario, Cycle]] = []
        with self.lock:
            if self.scenario_by_cycle is None:
                self.scenario_by_cycle = get_cycles_scenarios()
            if owner_id:
                if owner_id == "GLOBAL":
                    for cycle, scenarios in self.scenario_by_cycle.items():
                        if cycle is None:
                            cycles_scenarios.extend(scenarios)
                        else:
                            cycles_scenarios.append(cycle)
                elif is_readable(t.cast(ScenarioId, owner_id)):
                    entity = core_get(owner_id)
                    if entity and (scenarios_cycle := self.scenario_by_cycle.get(t.cast(Cycle, entity))):
                        cycles_scenarios.extend(scenarios_cycle)
                    elif isinstance(entity, Scenario):
                        cycles_scenarios.append(entity)
        return cycles_scenarios

    def get_data_node_history(self, datanode: DataNode, id: str):
        if (
            id
            and isinstance(datanode, DataNode)
            and id == datanode.id
            and (dn := core_get(id))
            and isinstance(dn, DataNode)
        ):
            res = []
            for e in dn.edits:
                job_id = e.get("job_id")
                job: t.Optional[Job] = None
                if job_id:
                    if not is_readable(job_id):
                        job_id += " not readable"
                    else:
                        job = core_get(job_id)
                res.append(
                    (
                        e.get("timestamp"),
                        job_id if job_id else e.get("writer_identifier", ""),
                        f"Execution of task {job.task.get_simple_label()}."
                        if job and job.task
                        else e.get("comment", ""),
                    )
                )
            return list(reversed(sorted(res, key=lambda r: r[0])))
        return _DoNotUpdate()

    def get_data_node_data(self, datanode: DataNode, id: str):
        if (
            id
            and isinstance(datanode, DataNode)
            and id == datanode.id
            and (dn := core_get(id))
            and isinstance(dn, DataNode)
        ):
            if dn._last_edit_date:
                if isinstance(dn, _AbstractTabularDataNode):
                    return (None, None, True, None)
                try:
                    value = dn.read()
                    if isinstance(value, (pd.DataFrame, pd.Series)):
                        return (None, None, True, None)
                    return (
                        value,
                        "date"
                        if "date" in type(value).__name__
                        else type(value).__name__
                        if isinstance(value, Number)
                        else None,
                        None,
                        None,
                    )
                except Exception as e:
                    return (None, None, None, f"read data_node: {e}")
            return (None, None, None, f"Data unavailable for {dn.get_simple_label()}")
        return _DoNotUpdate()

    def __check_readable_editable(self, state: State, id: str, type: str, var: str):
        if not is_readable(t.cast(DataNodeId, id)):
            state.assign(var, f"{type} {id} is not readable.")
            return False
        if not is_editable(t.cast(DataNodeId, id)):
            state.assign(var, f"{type} {id} is not editable.")
            return False
        return True

    def update_data(self, state: State, id: str, payload: t.Dict[str, str]):
        args = payload.get("args")
        if args is None or not isinstance(args, list) or len(args) < 1 or not isinstance(args[0], dict):
            return
        data = args[0]
        entity_id = data.get(_GuiCoreContext.__PROP_ENTITY_ID)
        if not self.__check_readable_editable(state, entity_id, "DataNode", _GuiCoreContext._DATANODE_VIZ_ERROR_VAR):
            return
        entity: DataNode = core_get(entity_id)
        if isinstance(entity, DataNode):
            try:
                entity.write(
                    parser.parse(data.get("value"))
                    if data.get("type") == "date"
                    else int(data.get("value"))
                    if data.get("type") == "int"
                    else float(data.get("value"))
                    if data.get("type") == "float"
                    else data.get("value"),
                    comment=data.get(_GuiCoreContext.__PROP_ENTITY_COMMENT),
                )
                entity.unlock_edit(self.gui._get_client_id())
                state.assign(_GuiCoreContext._DATANODE_VIZ_ERROR_VAR, "")
            except Exception as e:
                state.assign(_GuiCoreContext._DATANODE_VIZ_ERROR_VAR, f"Error updating Datanode value. {e}")
            state.assign(_GuiCoreContext._DATANODE_VIZ_DATA_ID_VAR, entity_id)  # this will update the data value

    def tabular_data_edit(self, state: State, var_name: str, payload: dict):
        user_data = payload.get("user_data", {})
        dn_id = user_data.get("dn_id")
        if not self.__check_readable_editable(state, dn_id, "DataNode", _GuiCoreContext._DATANODE_VIZ_ERROR_VAR):
            return
        datanode = core_get(dn_id) if dn_id else None
        if isinstance(datanode, DataNode):
            try:
                idx = payload.get("index")
                col = payload.get("col")
                tz = payload.get("tz")
                val = (
                    parser.parse(str(payload.get("value"))).astimezone(zoneinfo.ZoneInfo(tz)).replace(tzinfo=None)
                    if tz is not None
                    else payload.get("value")
                )
                # user_value = payload.get("user_value")
                data = self.__read_tabular_data(datanode)
                if hasattr(data, "at"):
                    data.at[idx, col] = val
                    datanode.write(data, comment=user_data.get(_GuiCoreContext.__PROP_ENTITY_COMMENT))
                    state.assign(_GuiCoreContext._DATANODE_VIZ_ERROR_VAR, "")
                else:
                    state.assign(
                        _GuiCoreContext._DATANODE_VIZ_ERROR_VAR,
                        "Error updating Datanode tabular value: type does not support at[] indexer.",
                    )
            except Exception as e:
                state.assign(_GuiCoreContext._DATANODE_VIZ_ERROR_VAR, f"Error updating Datanode tabular value. {e}")
        setattr(state, _GuiCoreContext._DATANODE_VIZ_DATA_ID_VAR, dn_id)

    def __read_tabular_data(self, datanode: DataNode):
        return datanode.read()

    def get_data_node_tabular_data(self, datanode: DataNode, id: str):
        if (
            id
            and isinstance(datanode, DataNode)
            and id == datanode.id
            and is_readable(t.cast(DataNodeId, id))
            and (dn := core_get(id))
            and isinstance(dn, DataNode)
            and dn.is_ready_for_reading
        ):
            try:
                return self.__read_tabular_data(dn)
            except Exception:
                return None
        return None

    def get_data_node_tabular_columns(self, datanode: DataNode, id: str):
        if (
            id
            and isinstance(datanode, DataNode)
            and id == datanode.id
            and is_readable(t.cast(DataNodeId, id))
            and (dn := core_get(id))
            and isinstance(dn, DataNode)
            and dn.is_ready_for_reading
        ):
            try:
                return self.gui._tbl_cols(
                    True, True, "{}", json.dumps({"data": "tabular_data"}), tabular_data=self.__read_tabular_data(dn)
                )
            except Exception:
                return None
        return None

    def get_data_node_chart_config(self, datanode: DataNode, id: str):
        if (
            id
            and isinstance(datanode, DataNode)
            and id == datanode.id
            and is_readable(t.cast(DataNodeId, id))
            and (dn := core_get(id))
            and isinstance(dn, DataNode)
            and dn.is_ready_for_reading
        ):
            try:
                return self.gui._chart_conf(
                    True, True, "{}", json.dumps({"data": "tabular_data"}), tabular_data=self.__read_tabular_data(dn)
                )
            except Exception:
                return None
        return None

    def select_id(self, state: State, id: str, payload: t.Dict[str, str]):
        args = payload.get("args")
        if args is None or not isinstance(args, list) or len(args) == 0 and isinstance(args[0], dict):
            return
        data = args[0]
        if owner_id := data.get("owner_id"):
            state.assign(_GuiCoreContext._DATANODE_VIZ_OWNER_ID_VAR, owner_id)
        elif history_id := data.get("history_id"):
            state.assign(_GuiCoreContext._DATANODE_VIZ_HISTORY_ID_VAR, history_id)
        elif data_id := data.get("data_id"):
            state.assign(_GuiCoreContext._DATANODE_VIZ_DATA_ID_VAR, data_id)
        elif chart_id := data.get("chart_id"):
            state.assign(_GuiCoreContext._DATANODE_VIZ_DATA_CHART_ID_VAR, chart_id)


# TODO remove when Submission is supported by Core API
def is_readable_submission(id: str):
    return _SubmissionManagerFactory._build_manager()._is_readable(t.cast(Submission, id))


def core_get_submission(id: str):
    return _SubmissionManagerFactory._build_manager()._get(id)
