import copy
import datetime
import functools
import logging
import random
import tempfile
import threading
from collections import namedtuple
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    Iterable,
    List,
    Mapping,
    MutableMapping,
    MutableSequence,
    Optional,
    Sequence,
    Tuple,
    Union,
    cast,
)
from uuid import UUID

from ruamel.yaml.comments import CommentedMap
from schema_salad import validate
from schema_salad.sourceline import SourceLine, indent
from schema_salad.utils import json_dumps

from . import command_line_tool, context, expression, procgenerator
from .builder import content_limit_respected_read
from .checker import can_assign_src_to_sink, static_checker
from .command_line_tool import CallbackJob, ExpressionTool
from .context import LoadingContext, RuntimeContext, getdefault
from .errors import WorkflowException
from .job import JobBase
from .load_tool import load_tool
from .loghandler import _logger
from .pathmapper import adjustDirObjs, get_listing
from .process import Process, get_overrides, shortname, uniquename
from .provenance import ProvenanceProfile
from .stdfsaccess import StdFsAccess
from .utils import DEFAULT_TMP_PREFIX, aslist

WorkflowStateItem = namedtuple("WorkflowStateItem", ["parameter", "value", "success"])


def default_make_tool(
    toolpath_object: MutableMapping[str, Any], loadingContext: LoadingContext
) -> Process:
    if not isinstance(toolpath_object, MutableMapping):
        raise WorkflowException("Not a dict: '%s'" % toolpath_object)
    if "class" in toolpath_object:
        if toolpath_object["class"] == "CommandLineTool":
            return command_line_tool.CommandLineTool(toolpath_object, loadingContext)
        if toolpath_object["class"] == "ExpressionTool":
            return command_line_tool.ExpressionTool(toolpath_object, loadingContext)
        if toolpath_object["class"] == "Workflow":
            return Workflow(toolpath_object, loadingContext)
        if toolpath_object["class"] == "ProcessGenerator":
            return procgenerator.ProcessGenerator(toolpath_object, loadingContext)
        if toolpath_object["class"] == "Operation":
            return command_line_tool.AbstractOperation(toolpath_object, loadingContext)

    raise WorkflowException(
        "Missing or invalid 'class' field in "
        "%s, expecting one of: CommandLineTool, ExpressionTool, Workflow"
        % toolpath_object["id"]
    )


context.default_make_tool = default_make_tool


def findfiles(
    wo: Any, fn: Optional[List[MutableMapping[str, Any]]] = None
) -> List[MutableMapping[str, Any]]:
    if fn is None:
        fn = []
    if isinstance(wo, MutableMapping):
        if wo.get("class") == "File":
            fn.append(wo)
            findfiles(wo.get("secondaryFiles", None), fn)
        else:
            for w in wo.values():
                findfiles(w, fn)
    elif isinstance(wo, MutableSequence):
        for w in wo:
            findfiles(w, fn)
    return fn


def match_types(
    sinktype,  # type: Union[List[str], str]
    src,  # type: WorkflowStateItem
    iid,  # type: str
    inputobj,  # type: Dict[str, Any]
    linkMerge,  # type: str
    valueFrom,  # type: Optional[str]
):  # type: (...) -> bool
    if isinstance(sinktype, MutableSequence):
        # Sink is union type
        for st in sinktype:
            if match_types(st, src, iid, inputobj, linkMerge, valueFrom):
                return True
    elif isinstance(src.parameter["type"], MutableSequence):
        # Source is union type
        # Check that at least one source type is compatible with the sink.
        original_types = src.parameter["type"]
        for source_type in original_types:
            src.parameter["type"] = source_type
            match = match_types(sinktype, src, iid, inputobj, linkMerge, valueFrom)
            if match:
                src.parameter["type"] = original_types
                return True
        src.parameter["type"] = original_types
        return False
    elif linkMerge:
        if iid not in inputobj:
            inputobj[iid] = []
        if linkMerge == "merge_nested":
            inputobj[iid].append(src.value)
        elif linkMerge == "merge_flattened":
            if isinstance(src.value, MutableSequence):
                inputobj[iid].extend(src.value)
            else:
                inputobj[iid].append(src.value)
        else:
            raise WorkflowException("Unrecognized linkMerge enum '%s'" % linkMerge)
        return True
    elif (
        valueFrom is not None
        or can_assign_src_to_sink(src.parameter["type"], sinktype)
        or sinktype == "Any"
    ):
        # simply assign the value from state to input
        inputobj[iid] = copy.deepcopy(src.value)
        return True
    return False


def object_from_state(
    state,  # type: Dict[str, Optional[WorkflowStateItem]]
    parms,  # type: List[Dict[str, Any]]
    frag_only,  # type: bool
    supportsMultipleInput,  # type: bool
    sourceField,  # type: str
    incomplete=False,  # type: bool
):  # type: (...) -> Optional[MutableMapping[str, Any]]
    inputobj = {}  # type: Dict[str, Any]
    for inp in parms:
        iid = inp["id"]
        if frag_only:
            iid = shortname(iid)
        if sourceField in inp:
            connections = aslist(inp[sourceField])
            if len(connections) > 1 and not supportsMultipleInput:
                raise WorkflowException(
                    "Workflow contains multiple inbound links to a single "
                    "parameter but MultipleInputFeatureRequirement is not "
                    "declared."
                )
            for src in connections:
                a_state = state.get(src, None)
                if a_state is not None and (
                    a_state.success in ("success", "skipped") or incomplete
                ):
                    if not match_types(
                        inp["type"],
                        a_state,
                        iid,
                        inputobj,
                        inp.get(
                            "linkMerge",
                            ("merge_nested" if len(connections) > 1 else None),
                        ),
                        valueFrom=inp.get("valueFrom"),
                    ):
                        raise WorkflowException(
                            "Type mismatch between source '%s' (%s) and "
                            "sink '%s' (%s)"
                            % (src, a_state.parameter["type"], inp["id"], inp["type"])
                        )
                elif src not in state:
                    raise WorkflowException(
                        "Connect source '%s' on parameter '%s' does not "
                        "exist" % (src, inp["id"])
                    )
                elif not incomplete:
                    return None

        if "pickValue" in inp and isinstance(inputobj.get(iid), MutableSequence):
            seq = cast(MutableSequence[Any], inputobj.get(iid))
            if inp["pickValue"] == "first_non_null":
                found = False
                for v in seq:
                    if v is not None:
                        found = True
                        inputobj[iid] = v
                        break
                if not found:
                    raise WorkflowException(
                        u"All sources for '%s' are null" % (shortname(inp["id"]))
                    )
            elif inp["pickValue"] == "only_non_null":
                found = False
                for v in seq:
                    if v is not None:
                        if found:
                            raise WorkflowException(
                                u"Expected only one source for '%s' to be non-null, got %s"
                                % (shortname(inp["id"]), seq)
                            )
                        found = True
                        inputobj[iid] = v
                if not found:
                    raise WorkflowException(
                        u"All sources for '%s' are null" % (shortname(inp["id"]))
                    )
            elif inp["pickValue"] == "all_non_null":
                inputobj[iid] = [v for v in seq if v is not None]

        if inputobj.get(iid) is None and "default" in inp:
            inputobj[iid] = inp["default"]

        if iid not in inputobj and ("valueFrom" in inp or incomplete):
            inputobj[iid] = None

        if iid not in inputobj:
            raise WorkflowException("Value for %s not specified" % (inp["id"]))
    return inputobj


class WorkflowJobStep(object):
    def __init__(self, step):
        # type: (WorkflowStep) -> None
        """Initialize this WorkflowJobStep."""
        self.step = step
        self.tool = step.tool
        self.id = step.id
        self.submitted = False
        self.completed = False
        self.iterable = (
            None
        )  # type: Optional[Generator[Union[ExpressionTool.ExpressionJob, JobBase, CallbackJob, None], None, None]]
        self.name = uniquename("step %s" % shortname(self.id))
        self.prov_obj = step.prov_obj
        self.parent_wf = step.parent_wf

    def job(
        self,
        joborder,  # type: Mapping[str, str]
        output_callback,  # type: functools.partial[None]
        runtimeContext,  # type: RuntimeContext
    ):
        # type: (...) -> Generator[Union[ExpressionTool.ExpressionJob, JobBase, CallbackJob], None, None]
        runtimeContext = runtimeContext.copy()
        runtimeContext.part_of = self.name
        runtimeContext.name = shortname(self.id)

        _logger.info("[%s] start", self.name)

        for j in self.step.job(joborder, output_callback, runtimeContext):
            yield j


class WorkflowJob(object):
    def __init__(self, workflow, runtimeContext):
        # type: (Workflow, RuntimeContext) -> None
        """Initialize this WorkflowJob."""
        self.workflow = workflow
        self.prov_obj = None  # type: Optional[ProvenanceProfile]
        self.parent_wf = None  # type: Optional[ProvenanceProfile]
        self.tool = workflow.tool
        if runtimeContext.research_obj is not None:
            self.prov_obj = workflow.provenance_object
            self.parent_wf = workflow.parent_wf
        self.steps = [WorkflowJobStep(s) for s in workflow.steps]
        self.state = {}  # type: Dict[str, Optional[WorkflowStateItem]]
        self.processStatus = ""
        self.did_callback = False
        self.made_progress = None  # type: Optional[bool]

        if runtimeContext.outdir is not None:
            self.outdir = runtimeContext.outdir
        else:
            self.outdir = tempfile.mkdtemp(
                prefix=getdefault(runtimeContext.tmp_outdir_prefix, DEFAULT_TMP_PREFIX)
            )

        self.name = uniquename(
            "workflow {}".format(
                getdefault(
                    runtimeContext.name,
                    shortname(self.workflow.tool.get("id", "embedded")),
                )
            )
        )

        _logger.debug(
            "[%s] initialized from %s",
            self.name,
            self.tool.get("id", "workflow embedded in %s" % runtimeContext.part_of),
        )

    def do_output_callback(self, final_output_callback):
        # type: (Callable[[Any, Any], Any]) -> None

        supportsMultipleInput = bool(
            self.workflow.get_requirement("MultipleInputFeatureRequirement")[0]
        )

        wo = None  # type: Optional[MutableMapping[str, str]]
        try:
            wo = object_from_state(
                self.state,
                self.tool["outputs"],
                True,
                supportsMultipleInput,
                "outputSource",
                incomplete=True,
            )
        except WorkflowException as err:
            _logger.error(
                "[%s] Cannot collect workflow output: %s", self.name, str(err)
            )
            self.processStatus = "permanentFail"
        if (
            self.prov_obj
            and self.parent_wf
            and self.prov_obj.workflow_run_uri != self.parent_wf.workflow_run_uri
        ):
            process_run_id = None
            self.prov_obj.generate_output_prov(wo or {}, process_run_id, self.name)
            self.prov_obj.document.wasEndedBy(
                self.prov_obj.workflow_run_uri,
                None,
                self.prov_obj.engine_uuid,
                datetime.datetime.now(),
            )
            prov_ids = self.prov_obj.finalize_prov_profile(self.name)
            # Tell parent to associate our provenance files with our wf run
            self.parent_wf.activity_has_provenance(
                self.prov_obj.workflow_run_uri, prov_ids
            )

        _logger.info("[%s] completed %s", self.name, self.processStatus)
        if _logger.isEnabledFor(logging.DEBUG):
            _logger.debug("[%s] outputs %s", self.name, json_dumps(wo, indent=4))

        self.did_callback = True

        final_output_callback(wo, self.processStatus)

    def receive_output(
        self, step, outputparms, final_output_callback, jobout, processStatus
    ):
        # type: (WorkflowJobStep, List[Dict[str,str]], Callable[[Any, Any], Any], Dict[str,str], str) -> None

        for i in outputparms:
            if "id" in i:
                if i["id"] in jobout:
                    self.state[i["id"]] = WorkflowStateItem(
                        i, jobout[i["id"]], processStatus
                    )
                else:
                    _logger.error(
                        "[%s] Output is missing expected field %s", step.name, i["id"]
                    )
                    processStatus = "permanentFail"
        if _logger.isEnabledFor(logging.DEBUG):
            _logger.debug(
                "[%s] produced output %s", step.name, json_dumps(jobout, indent=4)
            )

        if processStatus not in ("success", "skipped"):
            if self.processStatus != "permanentFail":
                self.processStatus = processStatus

            _logger.warning("[%s] completed %s", step.name, processStatus)
        else:
            _logger.info("[%s] completed %s", step.name, processStatus)

        step.completed = True
        # Release the iterable related to this step to
        # reclaim memory.
        step.iterable = None
        self.made_progress = True

        completed = sum(1 for s in self.steps if s.completed)
        if completed == len(self.steps):
            self.do_output_callback(final_output_callback)

    def try_make_job(
        self,
        step,  # type: WorkflowJobStep
        final_output_callback,  # type: Callable[[Any, Any], Any]
        runtimeContext,  # type: RuntimeContext
    ):  # type: (...) -> Generator[Union[ExpressionTool.ExpressionJob, JobBase, CallbackJob, None], None, None]

        if step.submitted:
            return

        inputparms = step.tool["inputs"]
        outputparms = step.tool["outputs"]

        supportsMultipleInput = bool(
            self.workflow.get_requirement("MultipleInputFeatureRequirement")[0]
        )

        try:
            inputobj = object_from_state(
                self.state, inputparms, False, supportsMultipleInput, "source"
            )
            if inputobj is None:
                _logger.debug("[%s] job step %s not ready", self.name, step.id)
                return

            if step.submitted:
                return
            _logger.info("[%s] starting %s", self.name, step.name)

            callback = functools.partial(
                self.receive_output, step, outputparms, final_output_callback
            )

            valueFrom = {
                i["id"]: i["valueFrom"] for i in step.tool["inputs"] if "valueFrom" in i
            }

            loadContents = set(
                i["id"] for i in step.tool["inputs"] if i.get("loadContents")
            )

            if len(valueFrom) > 0 and not bool(
                self.workflow.get_requirement("StepInputExpressionRequirement")[0]
            ):
                raise WorkflowException(
                    "Workflow step contains valueFrom but StepInputExpressionRequirement not in requirements"
                )

            vfinputs = {shortname(k): v for k, v in inputobj.items()}

            def postScatterEval(io):
                # type: (MutableMapping[str, Any]) -> Optional[MutableMapping[str, Any]]
                shortio = {shortname(k): v for k, v in io.items()}

                fs_access = getdefault(runtimeContext.make_fs_access, StdFsAccess)("")
                for k, v in io.items():
                    if k in loadContents and v.get("contents") is None:
                        with fs_access.open(v["location"], "rb") as f:
                            v["contents"] = content_limit_respected_read(f)

                def valueFromFunc(k, v):  # type: (Any, Any) -> Any
                    if k in valueFrom:
                        adjustDirObjs(
                            v, functools.partial(get_listing, fs_access, recursive=True)
                        )
                        return expression.do_eval(
                            valueFrom[k],
                            shortio,
                            self.workflow.requirements,
                            None,
                            None,
                            {},
                            context=v,
                            debug=runtimeContext.debug,
                            js_console=runtimeContext.js_console,
                            timeout=runtimeContext.eval_timeout,
                        )
                    return v

                psio = {k: valueFromFunc(k, v) for k, v in io.items()}
                if "when" in step.tool:
                    evalinputs = {shortname(k): v for k, v in psio.items()}
                    whenval = expression.do_eval(
                        step.tool["when"],
                        evalinputs,
                        self.workflow.requirements,
                        None,
                        None,
                        {},
                        context=v,
                        debug=runtimeContext.debug,
                        js_console=runtimeContext.js_console,
                        timeout=runtimeContext.eval_timeout,
                    )
                    if whenval is True:
                        pass
                    elif whenval is False:
                        _logger.debug(
                            "[%s] conditional %s evaluated to %s",
                            step.name,
                            step.tool["when"],
                            whenval,
                        )
                        _logger.debug(
                            "[%s] inputs was %s",
                            step.name,
                            json_dumps(evalinputs, indent=2),
                        )
                        return None
                    else:
                        raise WorkflowException(
                            "Conditional 'when' must evaluate to 'true' or 'false'"
                        )
                return psio

            if "scatter" in step.tool:
                scatter = aslist(step.tool["scatter"])
                method = step.tool.get("scatterMethod")
                if method is None and len(scatter) != 1:
                    raise WorkflowException(
                        "Must specify scatterMethod when scattering over multiple inputs"
                    )
                runtimeContext = runtimeContext.copy()
                runtimeContext.postScatterEval = postScatterEval

                emptyscatter = [shortname(s) for s in scatter if len(inputobj[s]) == 0]
                if emptyscatter:
                    _logger.warning(
                        "[job %s] Notice: scattering over empty input in "
                        "'%s'.  All outputs will be empty.",
                        step.name,
                        "', '".join(emptyscatter),
                    )

                if method == "dotproduct" or method is None:
                    jobs = dotproduct_scatter(
                        step, inputobj, scatter, callback, runtimeContext
                    )
                elif method == "nested_crossproduct":
                    jobs = nested_crossproduct_scatter(
                        step, inputobj, scatter, callback, runtimeContext
                    )
                elif method == "flat_crossproduct":
                    jobs = flat_crossproduct_scatter(
                        step, inputobj, scatter, callback, runtimeContext
                    )
            else:
                if _logger.isEnabledFor(logging.DEBUG):
                    _logger.debug(
                        u"[%s] job input %s", step.name, json_dumps(inputobj, indent=4)
                    )

                inputobj = postScatterEval(inputobj)
                if inputobj is not None:
                    if _logger.isEnabledFor(logging.DEBUG):
                        _logger.debug(
                            u"[%s] evaluated job input to %s",
                            step.name,
                            json_dumps(inputobj, indent=4),
                        )
                    jobs = step.job(inputobj, callback, runtimeContext)
                else:
                    _logger.info(u"[%s] will be skipped", step.name)
                    callback({k["id"]: None for k in outputparms}, "skipped")
                    step.completed = True
                    jobs = (_ for _ in ())

            step.submitted = True

            for j in jobs:
                yield j
        except WorkflowException:
            raise
        except Exception:
            _logger.exception("Unhandled exception")
            self.processStatus = "permanentFail"
            step.completed = True

    def run(
        self,
        runtimeContext,  # type: RuntimeContext
        tmpdir_lock=None,  # type: Optional[threading.Lock]
    ):  # type: (...) -> None
        """Log the start of each workflow."""
        _logger.info("[%s] start", self.name)

    def job(
        self,
        joborder,  # type: Mapping[str, Any]
        output_callback,  # type: Callable[[Any, Any], Any]
        runtimeContext,  # type: RuntimeContext
    ):  # type: (...) -> Generator[Union[ExpressionTool.ExpressionJob, JobBase, CallbackJob, None], None, None]
        self.state = {}
        self.processStatus = "success"

        if _logger.isEnabledFor(logging.DEBUG):
            _logger.debug("[%s] inputs %s", self.name, json_dumps(joborder, indent=4))

        runtimeContext = runtimeContext.copy()
        runtimeContext.outdir = None

        for index, inp in enumerate(self.tool["inputs"]):
            with SourceLine(
                self.tool["inputs"],
                index,
                WorkflowException,
                _logger.isEnabledFor(logging.DEBUG),
            ):
                inp_id = shortname(inp["id"])
                if inp_id in joborder:
                    self.state[inp["id"]] = WorkflowStateItem(
                        inp, joborder[inp_id], "success"
                    )
                elif "default" in inp:
                    self.state[inp["id"]] = WorkflowStateItem(
                        inp, inp["default"], "success"
                    )
                else:
                    raise WorkflowException(
                        "Input '%s' not in input object and does not have a "
                        " default value." % (inp["id"])
                    )

        for step in self.steps:
            for out in step.tool["outputs"]:
                self.state[out["id"]] = None

        completed = 0
        while completed < len(self.steps):
            self.made_progress = False

            for step in self.steps:
                if (
                    getdefault(runtimeContext.on_error, "stop") == "stop"
                    and self.processStatus != "success"
                ):
                    break

                if not step.submitted:
                    try:
                        step.iterable = self.try_make_job(
                            step, output_callback, runtimeContext
                        )
                    except WorkflowException as exc:
                        _logger.error("[%s] Cannot make job: %s", step.name, str(exc))
                        _logger.debug("", exc_info=True)
                        self.processStatus = "permanentFail"

                if step.iterable is not None:
                    try:
                        for newjob in step.iterable:
                            if (
                                getdefault(runtimeContext.on_error, "stop") == "stop"
                                and self.processStatus != "success"
                            ):
                                break
                            if newjob is not None:
                                self.made_progress = True
                                yield newjob
                            else:
                                break
                    except WorkflowException as exc:
                        _logger.error("[%s] Cannot make job: %s", step.name, str(exc))
                        _logger.debug("", exc_info=True)
                        self.processStatus = "permanentFail"

            completed = sum(1 for s in self.steps if s.completed)

            if not self.made_progress and completed < len(self.steps):
                if self.processStatus != "success":
                    break
                else:
                    yield None

        if not self.did_callback:
            # could have called earlier on line 336;
            self.do_output_callback(output_callback)
            # depends which one comes first. All steps are completed
            # or all outputs have been produced.


class Workflow(Process):
    def __init__(
        self,
        toolpath_object,  # type: MutableMapping[str, Any]
        loadingContext,  # type: LoadingContext
    ):  # type: (...) -> None
        """Initializet this Workflow."""
        super(Workflow, self).__init__(toolpath_object, loadingContext)
        self.provenance_object = None  # type: Optional[ProvenanceProfile]
        if loadingContext.research_obj is not None:
            run_uuid = None  # type: Optional[UUID]
            is_master = not loadingContext.prov_obj  # Not yet set
            if is_master:
                run_uuid = loadingContext.research_obj.ro_uuid

            self.provenance_object = ProvenanceProfile(
                loadingContext.research_obj,
                full_name=loadingContext.cwl_full_name,
                host_provenance=loadingContext.host_provenance,
                user_provenance=loadingContext.user_provenance,
                orcid=loadingContext.orcid,
                run_uuid=run_uuid,
                fsaccess=loadingContext.research_obj.fsaccess,
            )  # inherit RO UUID for master wf run
            # TODO: Is Workflow(..) only called when we are the master workflow?
            self.parent_wf = self.provenance_object

        # FIXME: Won't this overwrite prov_obj for nested workflows?
        loadingContext.prov_obj = self.provenance_object
        loadingContext = loadingContext.copy()
        loadingContext.requirements = self.requirements
        loadingContext.hints = self.hints

        self.steps = []  # type: List[WorkflowStep]
        validation_errors = []
        for index, step in enumerate(self.tool.get("steps", [])):
            try:
                self.steps.append(
                    self.make_workflow_step(
                        step, index, loadingContext, loadingContext.prov_obj
                    )
                )
            except validate.ValidationException as vexc:
                if _logger.isEnabledFor(logging.DEBUG):
                    _logger.exception("Validation failed at")
                validation_errors.append(vexc)

        if validation_errors:
            raise validate.ValidationException(
                "\n".join(str(v) for v in validation_errors)
            )

        random.shuffle(self.steps)

        # statically validate data links instead of doing it at runtime.
        workflow_inputs = self.tool["inputs"]
        workflow_outputs = self.tool["outputs"]

        step_inputs = []  # type: List[Any]
        step_outputs = []  # type: List[Any]
        param_to_step = {}  # type: Dict[str, Dict[str, Any]]
        for step in self.steps:
            step_inputs.extend(step.tool["inputs"])
            step_outputs.extend(step.tool["outputs"])
            for s in step.tool["inputs"]:
                param_to_step[s["id"]] = step.tool
            for s in step.tool["outputs"]:
                param_to_step[s["id"]] = step.tool

        if getdefault(loadingContext.do_validate, True):
            static_checker(
                workflow_inputs,
                workflow_outputs,
                step_inputs,
                step_outputs,
                param_to_step,
            )

    def make_workflow_step(
        self,
        toolpath_object: Dict[str, Any],
        pos: int,
        loadingContext: LoadingContext,
        parentworkflowProv: Optional[ProvenanceProfile] = None,
    ):  # type: (...) -> WorkflowStep
        return WorkflowStep(toolpath_object, pos, loadingContext, parentworkflowProv)

    def job(
        self,
        job_order,  # type: Mapping[str, Any]
        output_callbacks,  # type: Callable[[Any, Any], Any]
        runtimeContext,  # type: RuntimeContext
    ):  # type: (...) -> Generator[Union[WorkflowJob, ExpressionTool.ExpressionJob, JobBase, CallbackJob, None], None, None]
        builder = self._init_job(job_order, runtimeContext)

        if runtimeContext.research_obj is not None:
            if runtimeContext.toplevel:
                # Record primary-job.json
                runtimeContext.research_obj.fsaccess = runtimeContext.make_fs_access("")
                runtimeContext.research_obj.create_job(builder.job, self.job)

        job = WorkflowJob(self, runtimeContext)
        yield job

        runtimeContext = runtimeContext.copy()
        runtimeContext.part_of = "workflow %s" % job.name
        runtimeContext.toplevel = False

        for wjob in job.job(builder.job, output_callbacks, runtimeContext):
            yield wjob

    def visit(self, op: Callable[[MutableMapping[str, Any]], Any]) -> None:
        op(self.tool)
        for step in self.steps:
            step.visit(op)


def used_by_step(step: MutableMapping[str, Any], shortinputid: str) -> bool:
    for st in step["in"]:
        if st.get("valueFrom"):
            if ("inputs.%s" % shortinputid) in st.get("valueFrom"):
                return True
    if step.get("when"):
        if ("inputs.%s" % shortinputid) in cast(str, step.get("when")):
            return True
    return False


class WorkflowStep(Process):
    def __init__(
        self,
        toolpath_object,  # type: Dict[str, Any]
        pos,  # type: int
        loadingContext,  # type: LoadingContext
        parentworkflowProv=None,  # type: Optional[ProvenanceProfile]
    ):  # type: (...) -> None
        """Initialize this WorkflowStep."""
        if "id" in toolpath_object:
            self.id = toolpath_object["id"]
        else:
            self.id = "#step" + str(pos)

        loadingContext = loadingContext.copy()

        loadingContext.requirements = copy.deepcopy(
            getdefault(loadingContext.requirements, [])
        )
        assert loadingContext.requirements is not None  # nosec
        loadingContext.requirements.extend(toolpath_object.get("requirements", []))
        loadingContext.requirements.extend(
            get_overrides(getdefault(loadingContext.overrides_list, []), self.id).get(
                "requirements", []
            )
        )

        hints = copy.deepcopy(getdefault(loadingContext.hints, []))
        hints.extend(toolpath_object.get("hints", []))
        loadingContext.hints = hints

        try:
            if isinstance(toolpath_object["run"], MutableMapping):
                self.embedded_tool = loadingContext.construct_tool_object(
                    toolpath_object["run"], loadingContext
                )  # type: Process
            else:
                loadingContext.metadata = {}
                self.embedded_tool = load_tool(toolpath_object["run"], loadingContext)
        except validate.ValidationException as vexc:
            if loadingContext.debug:
                _logger.exception("Validation exception")
            raise WorkflowException(
                "Tool definition %s failed validation:\n%s"
                % (toolpath_object["run"], indent(str(vexc)))
            ) from vexc

        validation_errors = []
        self.tool = toolpath_object = copy.deepcopy(toolpath_object)
        bound = set()
        for stepfield, toolfield in (("in", "inputs"), ("out", "outputs")):
            toolpath_object[toolfield] = []
            for index, step_entry in enumerate(toolpath_object[stepfield]):
                if isinstance(step_entry, str):
                    param = CommentedMap()  # type: CommentedMap
                    inputid = step_entry
                else:
                    param = CommentedMap(step_entry.items())
                    inputid = step_entry["id"]

                shortinputid = shortname(inputid)
                found = False
                for tool_entry in self.embedded_tool.tool[toolfield]:
                    frag = shortname(tool_entry["id"])
                    if frag == shortinputid:
                        # if the case that the step has a default for a parameter,
                        # we do not want the default of the tool to override it
                        step_default = None
                        if "default" in param and "default" in tool_entry:
                            step_default = param["default"]
                        param.update(tool_entry)
                        param["_tool_entry"] = tool_entry
                        if step_default is not None:
                            param["default"] = step_default
                        found = True
                        bound.add(frag)
                        break
                if not found:
                    if stepfield == "in":
                        param["type"] = "Any"
                        param["used_by_step"] = used_by_step(self.tool, shortinputid)
                        param["not_connected"] = True
                    else:
                        if isinstance(step_entry, Mapping):
                            step_entry_name = step_entry["id"]
                        else:
                            step_entry_name = step_entry
                        validation_errors.append(
                            SourceLine(self.tool["out"], index).makeError(
                                "Workflow step output '%s' does not correspond to"
                                % shortname(step_entry_name)
                            )
                            + "\n"
                            + SourceLine(self.embedded_tool.tool, "outputs").makeError(
                                "  tool output (expected '%s')"
                                % (
                                    "', '".join(
                                        [
                                            shortname(tool_entry["id"])
                                            for tool_entry in self.embedded_tool.tool[
                                                "outputs"
                                            ]
                                        ]
                                    )
                                )
                            )
                        )
                param["id"] = inputid
                param.lc.line = toolpath_object[stepfield].lc.data[index][0]
                param.lc.col = toolpath_object[stepfield].lc.data[index][1]
                param.lc.filename = toolpath_object[stepfield].lc.filename
                toolpath_object[toolfield].append(param)

        missing_values = []
        for _, tool_entry in enumerate(self.embedded_tool.tool["inputs"]):
            if shortname(tool_entry["id"]) not in bound:
                if "null" not in tool_entry["type"] and "default" not in tool_entry:
                    missing_values.append(shortname(tool_entry["id"]))

        if missing_values:
            validation_errors.append(
                SourceLine(self.tool, "in").makeError(
                    "Step is missing required parameter%s '%s'"
                    % (
                        "s" if len(missing_values) > 1 else "",
                        "', '".join(missing_values),
                    )
                )
            )

        if validation_errors:
            raise validate.ValidationException("\n".join(validation_errors))

        super(WorkflowStep, self).__init__(toolpath_object, loadingContext)

        if self.embedded_tool.tool["class"] == "Workflow":
            (feature, _) = self.get_requirement("SubworkflowFeatureRequirement")
            if not feature:
                raise WorkflowException(
                    "Workflow contains embedded workflow but "
                    "SubworkflowFeatureRequirement not in requirements"
                )

        if "scatter" in self.tool:
            (feature, _) = self.get_requirement("ScatterFeatureRequirement")
            if not feature:
                raise WorkflowException(
                    "Workflow contains scatter but ScatterFeatureRequirement "
                    "not in requirements"
                )

            inputparms = copy.deepcopy(self.tool["inputs"])
            outputparms = copy.deepcopy(self.tool["outputs"])
            scatter = aslist(self.tool["scatter"])

            method = self.tool.get("scatterMethod")
            if method is None and len(scatter) != 1:
                raise validate.ValidationException(
                    "Must specify scatterMethod when scattering over multiple inputs"
                )

            inp_map = {i["id"]: i for i in inputparms}
            for inp in scatter:
                if inp not in inp_map:
                    raise validate.ValidationException(
                        SourceLine(self.tool, "scatter").makeError(
                            "Scatter parameter '%s' does not correspond to "
                            "an input parameter of this step, expecting '%s'"
                            % (
                                shortname(inp),
                                "', '".join(shortname(k) for k in inp_map.keys()),
                            )
                        )
                    )

                inp_map[inp]["type"] = {"type": "array", "items": inp_map[inp]["type"]}

            if self.tool.get("scatterMethod") == "nested_crossproduct":
                nesting = len(scatter)
            else:
                nesting = 1

            for _ in range(0, nesting):
                for oparam in outputparms:
                    oparam["type"] = {"type": "array", "items": oparam["type"]}
            self.tool["inputs"] = inputparms
            self.tool["outputs"] = outputparms
        self.prov_obj = None  # type: Optional[ProvenanceProfile]
        if loadingContext.research_obj is not None:
            self.prov_obj = parentworkflowProv
            if self.embedded_tool.tool["class"] == "Workflow":
                self.parent_wf = self.embedded_tool.parent_wf
            else:
                self.parent_wf = self.prov_obj

    def receive_output(self, output_callback, jobout, processStatus):
        # type: (Callable[...,Any], Dict[str, str], str) -> None
        output = {}
        for i in self.tool["outputs"]:
            field = shortname(i["id"])
            if field in jobout:
                output[i["id"]] = jobout[field]
            else:
                processStatus = "permanentFail"
        output_callback(output, processStatus)

    def job(
        self,
        job_order,  # type: Mapping[str, str]
        output_callbacks,  # type: Callable[[Any, Any], Any]
        runtimeContext,  # type: RuntimeContext
    ):  # type: (...) -> Generator[Union[ExpressionTool.ExpressionJob, JobBase, CallbackJob], None, None]
        # initialize sub-workflow as a step in the parent profile

        if (
            self.embedded_tool.tool["class"] == "Workflow"
            and runtimeContext.research_obj
            and self.prov_obj
            and self.embedded_tool.provenance_object
        ):
            self.embedded_tool.parent_wf = self.prov_obj
            process_name = self.tool["id"].split("#")[1]
            self.prov_obj.start_process(
                process_name,
                datetime.datetime.now(),
                self.embedded_tool.provenance_object.workflow_run_uri,
            )

        step_input = {}
        for inp in self.tool["inputs"]:
            field = shortname(inp["id"])
            if not inp.get("not_connected"):
                step_input[field] = job_order[inp["id"]]

        try:
            for tool in self.embedded_tool.job(
                step_input,
                functools.partial(self.receive_output, output_callbacks),
                runtimeContext,
            ):
                yield tool
        except WorkflowException:
            _logger.error("Exception on step '%s'", runtimeContext.name)
            raise
        except Exception as exc:
            _logger.exception("Unexpected exception")
            raise WorkflowException(str(exc)) from exc

    def visit(self, op: Callable[[MutableMapping[str, Any]], Any]) -> None:
        self.embedded_tool.visit(op)


class ReceiveScatterOutput(object):
    def __init__(
        self,
        output_callback,  # type: Callable[..., Any]
        dest,  # type: Dict[str, List[Optional[str]]]
        total,  # type: int
    ):  # type: (...) -> None
        """Initialize."""
        self.dest = dest
        self.completed = 0
        self.processStatus = "success"
        self.total = total
        self.output_callback = output_callback
        self.steps = (
            []
        )  # type: List[Optional[Generator[Union[ExpressionTool.ExpressionJob, JobBase, CallbackJob, None], None, None]]]

    def receive_scatter_output(self, index, jobout, processStatus):
        # type: (int, Dict[str, str], str) -> None
        for key, val in jobout.items():
            self.dest[key][index] = val

        # Release the iterable related to this step to
        # reclaim memory.
        if self.steps:
            self.steps[index] = None

        if processStatus != "success":
            if self.processStatus != "permanentFail":
                self.processStatus = processStatus

        self.completed += 1

        if self.completed == self.total:
            self.output_callback(self.dest, self.processStatus)

    def setTotal(
        self,
        total: int,
        steps: List[
            Optional[
                Generator[
                    Union[ExpressionTool.ExpressionJob, JobBase, CallbackJob, None],
                    None,
                    None,
                ]
            ]
        ],
    ) -> None:
        self.total = total
        self.steps = steps
        if self.completed == self.total:
            self.output_callback(self.dest, self.processStatus)


def parallel_steps(steps, rc, runtimeContext):
    # type: (List[Optional[Generator[Union[ExpressionTool.ExpressionJob, JobBase, CallbackJob, None], None, None]]], ReceiveScatterOutput, RuntimeContext) -> Generator[Union[ExpressionTool.ExpressionJob, JobBase, CallbackJob, None], None, None]
    while rc.completed < rc.total:
        made_progress = False
        for index, step in enumerate(steps):
            if getdefault(
                runtimeContext.on_error, "stop"
            ) == "stop" and rc.processStatus not in ("success", "skipped"):
                break
            if step is None:
                continue
            try:
                for j in step:
                    if getdefault(
                        runtimeContext.on_error, "stop"
                    ) == "stop" and rc.processStatus not in ("success", "skipped"):
                        break
                    if j is not None:
                        made_progress = True
                        yield j
                    else:
                        break
                if made_progress:
                    break
            except WorkflowException as exc:
                _logger.error("Cannot make scatter job: %s", str(exc))
                _logger.debug("", exc_info=True)
                rc.receive_scatter_output(index, {}, "permanentFail")
        if not made_progress and rc.completed < rc.total:
            yield None


def dotproduct_scatter(
    process,  # type: WorkflowJobStep
    joborder,  # type: MutableMapping[str, Any]
    scatter_keys,  # type: MutableSequence[str]
    output_callback,  # type: Callable[..., Any]
    runtimeContext,  # type: RuntimeContext
):  # type: (...) -> Generator[Union[ExpressionTool.ExpressionJob, JobBase, CallbackJob, None], None, None]
    jobl = None  # type: Optional[int]
    for key in scatter_keys:
        if jobl is None:
            jobl = len(joborder[key])
        elif jobl != len(joborder[key]):
            raise WorkflowException(
                "Length of input arrays must be equal when performing "
                "dotproduct scatter."
            )
    if jobl is None:
        raise Exception("Impossible codepath")

    output = {}  # type: Dict[str,List[Optional[str]]]
    for i in process.tool["outputs"]:
        output[i["id"]] = [None] * jobl

    rc = ReceiveScatterOutput(output_callback, output, jobl)

    steps = (
        []
    )  # type: List[Optional[Generator[Union[ExpressionTool.ExpressionJob, JobBase, CallbackJob, None], None, None]]]
    for index in range(0, jobl):
        sjobo = copy.copy(joborder)  # type: Optional[MutableMapping[str, Any]]
        assert sjobo is not None  # nosec
        for key in scatter_keys:
            sjobo[key] = joborder[key][index]

        if runtimeContext.postScatterEval is not None:
            sjobo = runtimeContext.postScatterEval(sjobo)
        curriedcallback = functools.partial(rc.receive_scatter_output, index)
        if sjobo is not None:
            steps.append(process.job(sjobo, curriedcallback, runtimeContext))
        else:
            curriedcallback({}, "skipped")
            steps.append(None)

    rc.setTotal(jobl, steps)
    return parallel_steps(steps, rc, runtimeContext)


def nested_crossproduct_scatter(
    process: WorkflowJobStep,
    joborder: MutableMapping[str, Any],
    scatter_keys: MutableSequence[str],
    output_callback: Callable[..., Any],
    runtimeContext: RuntimeContext,
) -> Generator[
    Union[ExpressionTool.ExpressionJob, JobBase, CallbackJob, None], None, None
]:
    scatter_key = scatter_keys[0]
    jobl = len(joborder[scatter_key])
    output = {}  # type: Dict[str, List[Optional[str]]]
    for i in process.tool["outputs"]:
        output[i["id"]] = [None] * jobl

    rc = ReceiveScatterOutput(output_callback, output, jobl)

    steps = (
        []
    )  # type: List[Optional[Generator[Union[ExpressionTool.ExpressionJob, JobBase, CallbackJob, None], None, None]]]
    for index in range(0, jobl):
        sjob = copy.copy(joborder)  # type: Optional[MutableMapping[str, Any]]
        assert sjob is not None  # nosec
        sjob[scatter_key] = joborder[scatter_key][index]

        if len(scatter_keys) == 1:
            if runtimeContext.postScatterEval is not None:
                sjob = runtimeContext.postScatterEval(sjob)
            curriedcallback = functools.partial(rc.receive_scatter_output, index)
            if sjob is not None:
                steps.append(process.job(sjob, curriedcallback, runtimeContext))
            else:
                curriedcallback({}, "skipped")
                steps.append(None)
        else:
            steps.append(
                nested_crossproduct_scatter(
                    process,
                    sjob,
                    scatter_keys[1:],
                    functools.partial(rc.receive_scatter_output, index),
                    runtimeContext,
                )
            )

    rc.setTotal(jobl, steps)
    return parallel_steps(steps, rc, runtimeContext)


def crossproduct_size(joborder, scatter_keys):
    # type: (MutableMapping[str, Any], MutableSequence[str]) -> int
    scatter_key = scatter_keys[0]
    if len(scatter_keys) == 1:
        ssum = len(joborder[scatter_key])
    else:
        ssum = 0
        for _ in range(0, len(joborder[scatter_key])):
            ssum += crossproduct_size(joborder, scatter_keys[1:])
    return ssum


def flat_crossproduct_scatter(
    process: WorkflowJobStep,
    joborder: MutableMapping[str, Any],
    scatter_keys: MutableSequence[str],
    output_callback: Callable[..., Any],
    runtimeContext: RuntimeContext,
) -> Generator[
    Union[ExpressionTool.ExpressionJob, JobBase, CallbackJob, None], None, None
]:
    output = {}  # type: Dict[str, List[Optional[str]]]
    for i in process.tool["outputs"]:
        output[i["id"]] = [None] * crossproduct_size(joborder, scatter_keys)
    callback = ReceiveScatterOutput(output_callback, output, 0)
    (steps, total) = _flat_crossproduct_scatter(
        process, joborder, scatter_keys, callback, 0, runtimeContext
    )
    callback.setTotal(total, steps)
    return parallel_steps(steps, callback, runtimeContext)


def _flat_crossproduct_scatter(
    process,  # type: WorkflowJobStep
    joborder,  # type: MutableMapping[str, Any]
    scatter_keys,  # type: MutableSequence[str]
    callback,  # type: ReceiveScatterOutput
    startindex,  # type: int
    runtimeContext,  # type: RuntimeContext
):  # type: (...) -> Tuple[List[Optional[Generator[Union[ExpressionTool.ExpressionJob, JobBase, CallbackJob, None], None, None]]], int]
    """Inner loop."""
    scatter_key = scatter_keys[0]
    jobl = len(joborder[scatter_key])
    steps = (
        []
    )  # type: List[Optional[Generator[Union[ExpressionTool.ExpressionJob, JobBase, CallbackJob, None], None, None]]]
    put = startindex
    for index in range(0, jobl):
        sjob = copy.copy(joborder)  # type: Optional[MutableMapping[str, Any]]
        assert sjob is not None  # nosec
        sjob[scatter_key] = joborder[scatter_key][index]

        if len(scatter_keys) == 1:
            if runtimeContext.postScatterEval is not None:
                sjob = runtimeContext.postScatterEval(sjob)
            curriedcallback = functools.partial(callback.receive_scatter_output, put)
            if sjob is not None:
                steps.append(process.job(sjob, curriedcallback, runtimeContext))
            else:
                curriedcallback({}, "skipped")
                steps.append(None)
            put += 1
        else:
            (add, _) = _flat_crossproduct_scatter(
                process, sjob, scatter_keys[1:], callback, put, runtimeContext
            )
            put += len(add)
            steps.extend(add)

    return (steps, put)
