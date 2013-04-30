"""UI for kernel creation."""

from __future__ import division

__copyright__ = "Copyright (C) 2012 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""




import numpy as np
from loopy.symbolic import IdentityMapper
from loopy.kernel.data import Instruction, SubstitutionRule
import islpy as isl
from islpy import dim_type

import re


# {{{ unique name generation

def generate_unique_possibilities(prefix):
    yield prefix

    try_num = 0
    while True:
        yield "%s_%d" % (prefix, try_num)
        try_num += 1

class UniqueNameGenerator:
    def __init__(self, existing_names):
        self.existing_names = existing_names.copy()

    def is_name_conflicting(self, name):
        return name in self.existing_names

    def add_name(self, name):
        if self.is_name_conflicting(name):
            raise ValueError("name '%s' conflicts with existing names")
        self.existing_names.add(name)

    def add_names(self, names):
        for name in names:
            self.add_name(name)

    def __call__(self, based_on="var"):
        for var_name in generate_unique_possibilities(based_on):
            if not self.is_name_conflicting(var_name):
                break

        self.existing_names.add(var_name)
        return var_name

_IDENTIFIER_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b")

def _gather_identifiers(s):
    return set(_IDENTIFIER_RE.findall(s))

class MakeUnique:
    """A tag for a string that identifies a partial identifier that is to
    be made unique by the UI.
    """

    def __init__(self, name):
        self.name = name

# }}}

# {{{ domain parsing

def parse_domains(ctx, args_and_vars, domains, defines):
    result = []
    available_parameters = args_and_vars.copy()
    used_inames = set()

    for dom in domains:
        if isinstance(dom, str):
            dom, = expand_defines(dom, defines)

            if not dom.lstrip().startswith("["):
                # i.e. if no parameters are already given
                ids = _gather_identifiers(dom)
                parameters = ids & available_parameters
                dom = "[%s] -> %s" % (",".join(parameters), dom)

            try:
                dom = isl.BasicSet.read_from_str(ctx, dom)
            except:
                print "failed to parse domain '%s'" % dom
                raise
        else:
            assert isinstance(dom, (isl.Set, isl.BasicSet))
            # assert dom.get_ctx() == ctx

        for i_iname in xrange(dom.dim(dim_type.set)):
            iname = dom.get_dim_name(dim_type.set, i_iname)

            if iname is None:
                raise RuntimeError("domain '%s' provided no iname at index "
                        "%d (redefined iname?)" % (dom, i_iname))

            if iname in used_inames:
                raise RuntimeError("domain '%s' redefines iname '%s' "
                        "that is part of a previous domain" % (dom, iname))

            used_inames.add(iname)
            available_parameters.add(iname)

        result.append(dom)

    return result

# }}}

# {{{ expand defines

WORD_RE = re.compile(r"\b([a-zA-Z0-9_]+)\b")
BRACE_RE = re.compile(r"\$\{([a-zA-Z0-9_]+)\}")

def expand_defines(insn, defines, single_valued=True):
    replacements = [()]

    for find_regexp, replace_pattern in [
            (BRACE_RE, r"\$\{%s\}"),
            (WORD_RE, r"\b%s\b"),
            ]:

        for match in find_regexp.finditer(insn):
            word = match.group(1)

            try:
                value = defines[word]
            except KeyError:
                continue

            if isinstance(value, list):
                if single_valued:
                    raise ValueError("multi-valued macro expansion not allowed "
                            "in this context (when expanding '%s')" % word)

                replacements = [
                        rep+((replace_pattern % word, subval),)
                        for rep in replacements
                        for subval in value
                        ]
            else:
                replacements = [
                        rep+((replace_pattern % word, value),)
                        for rep in replacements]

    for rep in replacements:
        rep_value = insn
        for pattern, val in rep:
            rep_value = re.sub(pattern, str(val), rep_value)

        yield rep_value

def expand_defines_in_expr(expr, defines):
    from pymbolic.primitives import Variable
    from loopy.symbolic import parse

    def subst_func(var):
        if isinstance(var, Variable):
            try:
                var_value = defines[var.name]
            except KeyError:
                return None
            else:
                return parse(str(var_value))
        else:
            return None

    from loopy.symbolic import SubstitutionMapper
    return SubstitutionMapper(subst_func)(expr)

# }}}

# {{{ parse instructions

INSN_RE = re.compile(
        "\s*(?:\<(?P<temp_var_type>.*?)\>)?"
        "\s*(?P<lhs>.+?)\s*(?<!\:)=\s*(?P<rhs>.+?)"
        "\s*?(?:\{(?P<options>[\s\w=,:]+)\}\s*)?$"
        )
SUBST_RE = re.compile(
        r"^\s*(?P<lhs>.+?)\s*:=\s*(?P<rhs>.+)\s*$"
        )

def parse_insn(insn):
    insn_match = INSN_RE.match(insn)
    subst_match = SUBST_RE.match(insn)
    if insn_match is not None and subst_match is not None:
        raise RuntimeError("instruction parse error: %s" % insn)

    if insn_match is not None:
        groups = insn_match.groupdict()
    elif subst_match is not None:
        groups = subst_match.groupdict()
    else:
        raise RuntimeError("insn parse error")

    from loopy.symbolic import parse
    lhs = parse(groups["lhs"])
    rhs = parse(groups["rhs"])

    if insn_match is not None:
        insn_deps = set()
        insn_id = None
        priority = 0

        if groups["options"] is not None:
            for option in groups["options"].split(","):
                option = option.strip()
                if not option:
                    raise RuntimeError("empty option supplied")

                equal_idx = option.find("=")
                if equal_idx == -1:
                    opt_key = option
                    opt_value = None
                else:
                    opt_key = option[:equal_idx].strip()
                    opt_value = option[equal_idx+1:].strip()

                if opt_key == "id":
                    insn_id = opt_value
                elif opt_key == "priority":
                    priority = int(opt_value)
                elif opt_key == "dep":
                    insn_deps = set(opt_value.split(":"))
                else:
                    raise ValueError("unrecognized instruction option '%s'"
                            % opt_key)

        if groups["temp_var_type"] is not None:
            if groups["temp_var_type"]:
                temp_var_type = np.dtype(groups["temp_var_type"])
            else:
                from loopy import infer_type
                temp_var_type = infer_type
        else:
            temp_var_type = None

        from pymbolic.primitives import Variable, Subscript
        if not isinstance(lhs, (Variable, Subscript)):
            raise RuntimeError("left hand side of assignment '%s' must "
                    "be variable or subscript" % lhs)

        return Instruction(
                    id=insn_id,
                    insn_deps=insn_deps,
                    forced_iname_deps=frozenset(),
                    assignee=lhs, expression=rhs,
                    temp_var_type=temp_var_type,
                    priority=priority)

    elif subst_match is not None:
        from pymbolic.primitives import Variable, Call

        if isinstance(lhs, Variable):
            subst_name = lhs.name
            arg_names = []
        elif isinstance(lhs, Call):
            if not isinstance(lhs.function, Variable):
                raise RuntimeError("Invalid substitution rule left-hand side")
            subst_name = lhs.function.name
            arg_names = []

            for i, arg in enumerate(lhs.parameters):
                if not isinstance(arg, Variable):
                    raise RuntimeError("Invalid substitution rule "
                                    "left-hand side: %s--arg number %d "
                                    "is not a variable"% (lhs, i))
                arg_names.append(arg.name)
        else:
            raise RuntimeError("Invalid substitution rule left-hand side")

        return SubstitutionRule(
                name=subst_name,
                arguments=tuple(arg_names),
                expression=rhs)

def parse_if_necessary(insn, defines):
    if isinstance(insn, Instruction):
        yield insn
        return
    elif not isinstance(insn, str):
        raise TypeError("Instructions must be either an Instruction "
                "instance or a parseable string. got '%s' instead."
                % type(insn))

    for insn in insn.split("\n"):
        comment_start = insn.find("#")
        if comment_start >= 0:
            insn = insn[:comment_start]

        insn = insn.strip()
        if not insn:
            continue

        for sub_insn in expand_defines(insn, defines, single_valued=False):
            yield parse_insn(sub_insn)

# }}}

# {{{ tag reduction inames as sequential

def tag_reduction_inames_as_sequential(knl):
    result = set()

    def map_reduction(red_expr, rec):
        rec(red_expr.expr)
        result.update(red_expr.inames)

    from loopy.symbolic import ReductionCallbackMapper
    for insn in knl.instructions:
        ReductionCallbackMapper(map_reduction)(insn.expression)

    from loopy.kernel.data import ParallelTag, ForceSequentialTag

    new_iname_to_tag = {}
    for iname in result:
        tag = knl.iname_to_tag.get(iname)
        if tag is not None and isinstance(tag, ParallelTag):
            raise RuntimeError("inconsistency detected: "
                    "reduction iname '%s' has "
                    "a parallel tag" % iname)

        if tag is None:
            new_iname_to_tag[iname] = ForceSequentialTag()

    from loopy import tag_inames
    return tag_inames(knl, new_iname_to_tag)

# }}}

# {{{ sanity checking

def check_for_duplicate_names(knl):
    name_to_source = {}

    def add_name(name, source):
        if name in name_to_source:
            raise RuntimeError("invalid %s name '%s'--name already used as "
                    "%s" % (source, name, name_to_source[name]))

        name_to_source[name] = source

    for name in knl.all_inames():
        add_name(name, "iname")
    for arg in knl.args:
        add_name(arg.name, "argument")
    for name in knl.temporary_variables:
        add_name(name, "temporary")
    for name in knl.substitutions:
        add_name(name, "substitution")

def check_for_nonexistent_iname_deps(knl):
    for insn in knl.instructions:
        if not set(insn.forced_iname_deps) <= knl.all_inames():
            raise ValueError("In instruction '%s': "
                    "cannot force dependency on inames '%s'--"
                    "they don't exist" % (
                        insn.id,
                        ",".join(
                            set(insn.forced_iname_deps)-knl.all_inames())))

def check_for_multiple_writes_to_loop_bounds(knl):
    from islpy import dim_type

    domain_parameters = set()
    for dom in knl.domains:
        domain_parameters.update(dom.get_space().get_var_dict(dim_type.param))

    temp_var_domain_parameters = domain_parameters & set(
            knl.temporary_variables)

    wmap = knl.writer_map()
    for tvpar in temp_var_domain_parameters:
        par_writers = wmap[tvpar]
        if len(par_writers) != 1:
            raise RuntimeError("there must be exactly one write to data-dependent "
                    "domain parameter '%s' (found %d)" % (tvpar, len(par_writers)))


def check_written_variable_names(knl):
    admissible_vars = (
            set(arg.name for arg in knl.args)
            | set(knl.temporary_variables.iterkeys()))

    for insn in knl.instructions:
        var_name = insn.get_assignee_var_name()

        if var_name not in admissible_vars:
            raise RuntimeError("variable '%s' not declared or not "
                    "allowed for writing" % var_name)

# }}}

# {{{ expand common subexpressions into assignments

class CSEToAssignmentMapper(IdentityMapper):
    def __init__(self, add_assignment):
        self.add_assignment = add_assignment
        self.expr_to_var = {}

    def map_common_subexpression(self, expr):
        try:
            return self.expr_to_var[expr.child]
        except KeyError:
            from loopy.symbolic import TypedCSE
            if isinstance(expr, TypedCSE):
                dtype = expr.dtype
            else:
                dtype = None

            child = self.rec(expr.child)
            from pymbolic.primitives import Variable
            if isinstance(child, Variable):
                return child

            var_name = self.add_assignment(expr.prefix, child, dtype)
            var = Variable(var_name)
            self.expr_to_var[expr.child] = var
            return var

def expand_cses(knl):
    def add_assignment(base_name, expr, dtype):
        if base_name is None:
            base_name = "var"

        new_var_name = var_name_gen(base_name)

        if dtype is None:
            from loopy import infer_type
            dtype = infer_type
        else:
            dtype=np.dtype(dtype)

        from loopy.kernel import TemporaryVariable
        new_temp_vars[new_var_name] = TemporaryVariable(
                name=new_var_name,
                dtype=dtype,
                is_local=None,
                shape=())

        from pymbolic.primitives import Variable
        insn = Instruction(
                id=knl.make_unique_instruction_id(extra_used_ids=newly_created_insn_ids),
                assignee=Variable(new_var_name), expression=expr)
        newly_created_insn_ids.add(insn.id)
        new_insns.append(insn)

        return new_var_name

    cseam = CSEToAssignmentMapper(add_assignment=add_assignment)

    new_insns = []

    var_name_gen = knl.get_var_name_generator()

    newly_created_insn_ids = set()
    new_temp_vars = knl.temporary_variables.copy()

    for insn in knl.instructions:
        new_insns.append(insn.copy(expression=cseam(insn.expression)))

    return knl.copy(
            instructions=new_insns,
            temporary_variables=new_temp_vars)

# }}}

# {{{ temporary variable creation

def create_temporaries(knl):
    new_insns = []
    new_temp_vars = knl.temporary_variables.copy()

    for insn in knl.instructions:
        from loopy.kernel.data import TemporaryVariable

        if insn.temp_var_type is not None:
            assignee_name = insn.get_assignee_var_name()

            assignee_indices = []
            from pymbolic.primitives import Variable
            for index_expr in insn.get_assignee_indices():
                if (not isinstance(index_expr, Variable)
                        or not index_expr.name in knl.all_inames()):
                    raise RuntimeError(
                            "only plain inames are allowed in "
                            "the lvalue index when declaring the "
                            "variable '%s' in an instruction"
                            % assignee_name)

                assignee_indices.append(index_expr.name)

            base_indices, shape = \
                    knl.find_var_base_indices_and_shape_from_inames(
                            assignee_indices, knl.cache_manager)

            if assignee_name in new_temp_vars:
                raise RuntimeError("cannot create temporary variable '%s'--"
                        "already exists" % assignee_name)
            if assignee_name in knl.arg_dict:
                raise RuntimeError("cannot create temporary variable '%s'--"
                        "already exists as argument" % assignee_name)

            new_temp_vars[assignee_name] = TemporaryVariable(
                    name=assignee_name,
                    dtype=insn.temp_var_type,
                    is_local=None,
                    base_indices=base_indices,
                    shape=shape)

            insn = insn.copy(temp_var_type=None)

        new_insns.append(insn)

    return knl.copy(
            instructions=new_insns,
            temporary_variables=new_temp_vars)

# }}}

# {{{ check for reduction iname duplication

def check_for_reduction_inames_duplication_requests(kernel):

    # {{{ helper function

    def check_reduction_inames(reduction_expr, rec):
        for iname in reduction_expr.inames:
            if iname.startswith("@"):
                raise RuntimeError("Reduction iname duplication with '@' is no "
                        "longer supported. Use loopy.duplicate_inames instead.")

    # }}}


    from loopy.symbolic import ReductionCallbackMapper
    rcm = ReductionCallbackMapper(check_reduction_inames)
    for insn in kernel.instructions:
        rcm(insn.expression)

    for sub_name, sub_rule in kernel.substitutions.iteritems():
        rcm(sub_rule.expression)

# }}}

# {{{ kernel creation top-level

def make_kernel(device, domains, instructions, kernel_args=[], *args, **kwargs):
    """User-facing kernel creation entrypoint."""

    for forbidden_kwarg in [
            "substitutions",
            "iname_slab_increments",
            "applied_iname_rewrites",
            "cache_manager",
            "isl_context",
            ]:
        if forbidden_kwarg in kwargs:
            raise RuntimeError("'%s' is not part of user-facing interface"
                    % forbidden_kwarg)

    defines = kwargs.get("defines", {})
    temporary_variables = kwargs.get("temporary_variables", {})

    # {{{ instruction/subst parsing

    parsed_instructions = []
    kwargs["substitutions"] = substitutions = {}

    if isinstance(instructions, str):
        instructions = [instructions]
    for insn in instructions:
        for new_insn in parse_if_necessary(insn, defines):
            if isinstance(new_insn, Instruction):
                parsed_instructions.append(new_insn)
            elif isinstance(new_insn, SubstitutionRule):
                substitutions[new_insn.name] = new_insn
            else:
                raise RuntimeError("unexpected type in instruction parsing")

    instructions = parsed_instructions
    del parsed_instructions

    # }}}

    # Ordering dependency:
    # Domain construction needs to know what temporary variables are
    # available. That information can only be obtained once instructions
    # are parsed.

    # {{{ parse domains

    if isinstance(domains, str):
        domains = [domains]

    isl_context = None
    for domain in domains:
        if isinstance(domain, isl.BasicSet):
            isl_context = domain.get_ctx()
    if isl_context is None:
        isl_context = isl.Context()

    from loopy.kernel.data import ValueArg
    scalar_arg_names = set(arg.name for arg in kernel_args if isinstance(arg, ValueArg))
    var_names = (
            set(temporary_variables)
            | set(insn.get_assignee_var_name()
                for insn in instructions
                if insn.temp_var_type is not None))
    domains = parse_domains(isl_context, scalar_arg_names | var_names, domains,
            defines)

    kwargs["isl_context"] = isl_context

    # }}}

    from loopy.kernel import LoopKernel
    knl = LoopKernel(device, domains, instructions, kernel_args, *args, **kwargs)

    check_for_nonexistent_iname_deps(knl)
    check_for_reduction_inames_duplication_requests(knl)

    knl = tag_reduction_inames_as_sequential(knl)
    knl = create_temporaries(knl)
    knl = expand_cses(knl)

    # -------------------------------------------------------------------------
    # Ordering dependency:
    # -------------------------------------------------------------------------
    # Must create temporaries before checking for writes to temporary variables
    # that are domain parameters.
    # -------------------------------------------------------------------------

    check_for_multiple_writes_to_loop_bounds(knl)
    check_for_duplicate_names(knl)
    check_written_variable_names(knl)

    return knl

# }}}

# vim: fdm=marker