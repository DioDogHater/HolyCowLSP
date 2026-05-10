import logging
import re
import attrs
import json
import enum

import time

from functools import reduce
import operator

from os import path

from pygls.cli import start_server
from pygls.lsp.server import LanguageServer
from pygls.workspace import TextDocument

from typing import Any
from lsprotocol import types

# Reference used - credits for some of the code written here:
#   https://pygls.readthedocs.io/en/latest/servers/examples/semantic-tokens.html

class TokenModifier(enum.IntFlag):
    deprecated = enum.auto()
    readonly = enum.auto()
    defaultLibrary = enum.auto()
    definition = enum.auto()

TokenTypes = [
    "keyword", "variable", "function", "operator", "parameter", "member", "type", "namespace", "comment", "macro", "number", "string"
]

# Preprocessor directives
MACRO = re.compile(r"#define\s+(\S+)\s*(.*)|#include\s+\"(.+)\"|#.*")

# Identifiers
IDENTIFIER = re.compile(r"[a-zA-Z_@][a-zA-Z_\d]*")

# Operators
OP = re.compile(r"\.{3}|[+\-<>&|]{2}|[<>!=~]=?|[+\-/*%]=|[~\{\}\[\]\(\)\.\?,+:;*/%\-=^&\|!]")

# To skip space
SPACE = re.compile(r"\s+")

# To skip comments
COMMENTS = re.compile(r"\/\/(.+)")
MLCOM_ONE = re.compile(r"\/\*(.*)\*\/")
MLCOM_START = re.compile(r"\/\*(.*)")
MLCOM_END = re.compile(r"(.*)\*\/")

# Type matching
INT_TYPE = re.compile(r"^[u]?int(8|16|32|64)?$")
MISC_TYPE = {"void":0, "char":1, "bool":1, "float":4, "double":8}

# Value
NUMBER = re.compile(r"0x[a-zA-Z\d]+|0o[0-7]+|0b[01]+|\d+(?:\.\d+)?")
STRING = re.compile(r"'.+'|\".*\"")
STRING_START = re.compile(r"\".*")
STRING_END = re.compile(r".*[^\\]?\"")

# Keywords
with open(path.join(path.dirname(__file__), "keywords.json"), "r") as f:
    KEYWORDS = json.load(f)

class Token:
    def __init__(self, line : int, offset : int, text : str, tk_type : str = "", modifiers : list[TokenModifier] = [], context : dict[str, Any] = {}.copy()):
        self.line : int = line
        self.offset : int = offset
        self.text : str = text
        self.tk_type : str = tk_type
        self.modifiers : list[TokenModifier] = modifiers
        self.context : dict[str, Any] = context

    def __repr__(self) -> str:
        return f"Token({self.line}, {self.offset}, \"{self.text}\", {self.tk_type}, {self.context})"

    def get_info(self) -> str:
        # TODO Display more useful info with full context

        def get_prot(mods : list[str]) -> str:
            prot : str = "\\+"
            if "private" in mods:
                prot = "\\-"
            elif "protected" in mods:
                prot = "\\#"
            if "@peek" in mods:
                prot += "(*peek*)"
            if "constexpr" in mods:
                prot += "(*const*)"
            if "virtual" in mods:
                prot += "(*virt*)"
            return prot

        if self.tk_type == "keyword":
            kw : dict = KEYWORDS[self.text]
            return f"### {kw.get('type', 'Keyword')} `{self.text}`\n*{kw.get('msg', '')}*\n```\n{'\n'.join(kw.get('ex', ['']))}\n```"

        elif self.tk_type == "type":
            s : str = f"## Type `{self.text}` (*{self.context.get('type', 'unknown')}*)\nsize = {self.context.get('size', '?')}, align = {self.context.get('align', '?')}"

            if "docs" in self.context and self.context["docs"]:
                s += f"\n\n{self.context['docs']}\n"

            if "parent" in self.context:
                s += f"\n\n*Inherits {self.context['parent']}*\n"

            if "members" in self.context and self.context["members"]:
                m : dict = self.context["members"]
                s += f"\n### Members\n{'\n\n'.join([get_prot(val.get('mods', []))+' **'+name+'** : *'+val.get('type', '?')+'*'+(f' = `{val['default_val'] if 'default_val' in val else val['val']}`' if 'default_val' in val or 'val' in val else '') for name, val in m.items()])}"

            if "methods" in self.context and self.context["methods"]:
                m : dict = self.context["methods"]
                s += f"\n### Methods\n{'\n\n'.join([get_prot(val.get('mods', []))+' *'+val.get('type', '?')+'* **'+name+'**('+val.get('params', '?')+')' for name, val in m.items()])}"

            return s

        elif self.tk_type == "namespace":
            s : str = f"## {self.context['type']} `{self.text}`"
            if self.context["type"] == "Module":
                if "docs" in self.context and self.context["docs"]:
                    s += f"\n{self.context['docs']}\n"

                if "vars" in self.context and self.context["vars"]:
                    m : dict = self.context["vars"]
                    s += f"\n***Members:***\n\n{'\n\n'.join([get_prot(val.get('mods', []))+' **'+name+'** : *'+val.get('type', '?')+'*'+(f' = `{val['val']}`' if 'val' in val else '') for name, val in m.items()])}\n"

                if "funcs" in self.context and self.context["funcs"]:
                    m : dict = self.context["funcs"]
                    s += f"\n***Methods:***\n\n{'\n\n'.join([get_prot(val.get('mods', []))+' *'+val.get('type', '?')+'* **'+name+'**('+val.get('params', '?')+')' for name, val in m.items()])}"
            elif self.context["type"] == "Enum":
                s += f"\n{'\n\n'.join([x + f' = {v['val']}' if 'val' in v else '' for x, v in self.context['vals'].items()])}"

            return s

        elif self.tk_type == "function":
            mods : str = " ".join(self.context["mods"])+" " if "mods" in self.context else ""
            s : str = f"```\n{mods}{self.context.get('type', '?')} {self.text}({self.context.get('params', '?')})\n```"
            if "docs" in self.context:
                s += "\n\n"+self.context["docs"]
            return s

        elif self.tk_type == "member" or self.tk_type == "variable":
            s : str = f"{'Member' if self.tk_type == 'member' else 'Var'} *{self.text}* (`{self.context.get('type', '?')}`)"
            if "val" in self.context:
                s += f" = `{self.context['val']}`"
            if "mods" in self.context:
                s += "\n\n*"+" ".join(self.context["mods"])+"* "
            if "docs" in self.context:
                s += "\n\n"+self.context["docs"]
            return s

        elif self.tk_type == "parameter":
            return f"Param *{self.text}* (`{self.context.get('type', '?')}`)"

        elif self.tk_type == "number":
            s : str = f"Number = `{self.text}`"
            try:
                if self.text.startswith("0x") or self.text.startswith("0b") or self.text.startswith("0o"):
                    s += f"\n\ndec = {int(self.text, base=0)}"
                else:
                    s += f"\n\nhex = 0x{int(self.text):X}"
            except: pass
            return s

        elif self.tk_type == "string":
            if self.text[0] == '"':
                return f"{self.text}\n\nlength = {len(self.text) - 2}"
            else:
                return f"{self.text}"

        elif self.tk_type == "macro":
            if self.context["type"] == "unknown":
                return "Unknown preprocessor directive"
            elif self.context["type"] == "define":
                return f"Macro *{self.context.get('name', 'unknown')}* = {self.context.get('val', 'unknown')}"
            elif self.context["type"] == "include":
                return f"Include *\"{self.context.get('dir', 'unknown')}\"*"
            elif self.context["type"] == "macro":
                return f"= `{self.context.get('val', '_')}`"
            else:
                return repr(self)

        elif self.tk_type == "comment":
            return f"{self.text}"

        else:
            return repr(self)

class HolyCowLS(LanguageServer):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.cache : dict[str, tuple[dict, dict, dict, dict, dict, dict]] = {}
        self.tokens : dict[str, list[Token]] = {}

    def __lex(self, doc : TextDocument) -> list[Token]:
        start_time = time.perf_counter()
        tks : list[Token] = []

        comment : Token | None = None
        s : Token | None = None

        for cur_line, line in enumerate(doc.lines):
            offset : int = 0

            while line:
                match : re.Match = None

                if comment:
                    if (match := MLCOM_END.match(line)):
                        comment.text += "\n\n" + match.group(1)
                        comment = None
                    else:
                        comment.text += "\n\n" + line
                        break

                if s:
                    if (match := STRING_END.match(line)):
                        s.text += "\n" + match.group(0)
                        s = None
                    else:
                        s.text += "\n" + line
                        break

                elif (match := SPACE.match(line)) is not None:
                    pass

                elif (match := COMMENTS.match(line)) is not None or (match := MLCOM_ONE.match(line)) is not None:
                    if len(tks) > 0 and tks[-1].tk_type == "comment" and tks[-1].line + tks[-1].text.count("\n") == cur_line - 1 and tks[-1].offset - 2 == offset:
                        tks[-1].text += "\n" + match.group(1)
                    else:
                        tks.append(Token(
                            line   = cur_line,
                            offset = offset + 2,
                            text   = match.group(1),
                            tk_type = "comment"
                        ))

                elif (match := MLCOM_START.match(line)) is not None:
                    if len(tks) > 0 and tks[-1].tk_type == "comment" and tks[-1].line + tks[-1].text.count("\n") == cur_line - 1 and tks[-1].offset - 2 == offset:
                        tks[-1].text += "\n" + match.group(1)
                        comment = tks[-1]
                        break

                    comment = Token(
                        line   = cur_line,
                        offset = offset + 2,
                        text   = match.group(1),
                        tk_type = "comment"
                    )
                    tks.append(comment)
                    break

                elif (match := OP.match(line)) is not None:
                    tks.append(Token(
                        line   = cur_line,
                        offset = offset,
                        text   = match.group(0),
                        tk_type = "operator"
                    ))

                elif (match := NUMBER.match(line)) is not None:
                    tks.append(Token(
                        line   = cur_line,
                        offset = offset,
                        text   = match.group(0),
                        tk_type = "number"
                    ))

                elif (match := STRING.match(line)) is not None:
                    tks.append(Token(
                        line   = cur_line,
                        offset = offset,
                        text   = match.group(0),
                        tk_type = "string"
                    ))

                elif (match := STRING_START.match(line)) is not None:
                    s : Token = Token(
                        line   = cur_line,
                        offset = offset,
                        text   = match.group(0),
                        tk_type = "string"
                    )
                    tks.append(s)

                elif (match := MACRO.match(line)) is not None:
                    macro : Token = Token(
                        line   = cur_line,
                        offset = offset,
                        text   = match.group(0),
                        tk_type = "macro"
                    )
                    if macro.text.startswith("#define"):
                        macro.modifiers.append(TokenModifier.definition)
                        macro.context = {"type":"define"}
                        if (name := match.group(1)):
                            macro.context["name"] = name
                        if (val := match.group(2)):
                            macro.context["val"] = val
                    elif macro.text.startswith("#include"):
                        macro.context = {"type":"include"}
                        if (include_dir := match.group(3)):
                            macro.context["dir"] = include_dir
                    else:
                        macro.context = {"type":"unknown"}
                    tks.append(macro)

                elif (match := IDENTIFIER.match(line)) is not None:
                    tks.append(Token(
                        line   = cur_line,
                        offset = offset,
                        text   = match.group(0)
                    ))

                else:
                    line = line[1:]
                    offset += 1
                    continue

                line = line[match.end():]
                offset += len(match.group(0))

        logging.log(logging.CRITICAL, f"Tokenized {doc.path} in {time.perf_counter() - start_time} seconds")
        return tks

    def __classify_tokens(self, tks : list[Token],
                          curr_path : str,
                          variables : dict,
                          functions : dict,
                          custom_types : dict,
                          modules : dict,
                          enums : dict,
                          macros : dict) -> list[Token]:
        start_time = time.perf_counter()
        idx : int = 0

        def tk_is(offset : int, tk_type : str | None = None, txt : str | tuple[str, ...] | None = None) -> bool:
            x = True
            if idx + offset < 0 or idx + offset >= len(tks):
                return False
            if tk_type:
                x = (x and (tks[idx + offset].tk_type == tk_type))
            elif txt and isinstance(txt, str):
                x = (x and (tks[idx + offset].text == txt))
            elif txt and isinstance(txt, (tuple, list)):
                x = (x and (tks[idx + offset].text in txt))
            return x

        def tk_get_type(offset : int) -> list[Token] | None:
            l : list[Token] = []
            while tk_is(offset, txt="*"):
                l.insert(0, tks[idx + offset])
                offset -= 1
            if tk_is(offset, tk_type="type"):
                l.insert(0, tks[idx + offset])
                return l
            return None

        def tk_until(offset : int, tk_type : str | None = None, txt : str | tuple[str, ...] | None = None) -> list[Token]:
            l : list[Token] = []
            while tk_is(offset) and not tk_is(offset, tk_type, txt):
                l.append(tks[idx + offset])
                offset += 1
            return l

        def tk_find(start : int, step : int, tk_type : str | None = None, txt : str | tuple[str, ...] | None = None) -> int:
            while tk_is(start) and not tk_is(start, tk_type, txt):
                start += step
            return start

        def tk_countdown(offset : int, text : tuple[str, str] = ('(', ')'), count : int = 1) -> list[Token]:
            l : list[Token] = []
            while count > 0 and tk_is(offset):
                l.append(tks[idx + offset])
                if tk_is(offset, txt=text[1]):
                    count -= 1
                elif tk_is(offset, txt=text[0]):
                    count += 1
                offset += 1
            return l[:-1]

        def extract_type(t : str) -> str:
            return t.removesuffix("[]").strip("*")

        def get_type(t : str) -> tuple[int, int]:
            t = t.removesuffix("[]")
            if t.endswith("*"):
                return (8, 8)
            t = extract_type(t)
            if (match := INT_TYPE.match(t)):
                sz : int = 0
                if match.group(1) is not None:
                    sz = int(match.group(1)) // 8
                else:
                    sz = 8
                return (sz, sz)
            elif t in MISC_TYPE:
                return (MISC_TYPE[t], MISC_TYPE[t])
            elif t in custom_types:
                return (custom_types[t].get('size', 0), custom_types[t].get('align', 0))
            else:
                return (0, 1)

        def align(x : int, n : int) -> int:
            if n == 0:
                return x
            return (x + n - 1) // n * n

        def transfer_dict(dest : dict, src : dict) -> None:
            """Transfers key / value pairs from src to dest, except those who have the same key as in dest."""
            for key, val in src.items():
                if not (key in dest):
                    dest[key] = val

        in_params : bool = False
        curr_func_type : str | None = None

        curr_module : str | None = None
        curr_enum : str | None = None
        curr_type : str | None = None

        paren_count : int = 0
        brace_count : int = 0

        for tk in tks:
            if tk.text == "{":
                brace_count += 1
            elif tk.text == "}":
                brace_count -= 1
                if brace_count < 1:
                    curr_module = curr_enum = curr_type = None

            if tk.text == "(":
                paren_count += 1
            elif tk.text == ")":
                paren_count -= 1
                if paren_count < 1:
                    in_params = False

            if tk.tk_type == "macro":
                if tk.context["type"] == "define" and "name" in tk.context:
                    macros[tk.context["name"]] = tk.context.get("val", "")
                elif tk.context["type"] == "include":
                    include_dir : str | None = tk.context.get("dir", None)
                    if include_dir:
                        real_path = path.join(path.dirname(curr_path), include_dir)

                        # Cached analysis
                        if real_path in self.cache:
                            # logging.log(logging.CRITICAL, f"USING CACHED : {real_path}")
                            vrs, fns, cts, mods, enms, mcrs = self.cache[real_path]
                            transfer_dict(variables, vrs)
                            transfer_dict(functions, fns)
                            transfer_dict(custom_types, cts)
                            transfer_dict(modules, mods)
                            transfer_dict(enums, enms)
                            transfer_dict(macros, mcrs)
                            idx += 1
                            continue

                        # First time analysing
                        source : str = ""
                        try:
                            with open(real_path, "r") as f:
                                source = f.read()
                        except IOError as e:
                            logging.log(logging.CRITICAL, f"FAILED TO LOAD INCLUDED FILE {real_path} : {e}")
                            tk.context["dir"] += " [Invalid path]"
                        else:
                            included_tks : list[Token] = self.__lex(TextDocument(include_dir, source))
                            self.__classify_tokens(included_tks, real_path, variables, functions, custom_types, modules, enums, macros)
                            # logging.log(logging.CRITICAL, f"ANALYSED INCLUDED FILE : {real_path}")

            if tk.tk_type:
                pass

            # Keywords
            elif tk.text in KEYWORDS:
                tk.tk_type = "keyword"

            # Integer types
            elif (match := INT_TYPE.match(tk.text)) is not None:
                tk.tk_type = "type"
                sz : int = 0
                if match.group(1) is not None:
                    sz = int(match.group(1)) // 8
                else:
                    sz = 8
                tk.context = {"type":"built-in", "size": sz, "align": sz}

            # Miscelaneous types
            elif tk.text in MISC_TYPE:
                tk.tk_type = "type"
                tk.context = {"type":"built-in", "size": MISC_TYPE[tk.text], "align": MISC_TYPE[tk.text]}

            # Module definition
            elif tk_is(-1, txt="module"):
                tk.tk_type = "namespace"
                if tk.text in modules:
                    tk.context = modules[tk.text]
                else:
                    tk.context = {"type":"Module", "funcs":{}, "vars":{}}
                    modules[tk.text] = tk.context

                if tk_is(-2, tk_type="comment"):
                    tk.context["docs"] = tks[idx - 2].text

                curr_func_type = None
                curr_module = tk.text
                brace_count = 0

            # Enum definition
            elif tk_is(-1, txt="enum"):
                tk.tk_type = "namespace"
                tk.context = {"type":"Enum", "vals":{}}
                enums[tk.text] = tk.context

                curr_func_type = None
                curr_enum = tk.text
                brace_count = 0

            # Enum constant
            elif curr_enum:
                tk.tk_type = "member"
                tk.context = {"type": "int"}
                if tk_is(1, txt="=") and tk_is(2, tk_type="number"):
                    tk.context["val"] = tks[idx + 2].text
                enums[curr_enum]["vals"][tk.text] = tk.context

            # Accessing modules / enums
            elif tk_is(1, txt=".") and ((tk.text in modules) or (tk.text in enums)):
                tk.tk_type = "namespace"
                tk.context = modules[tk.text] if tk.text in modules else enums[tk.text]

            # Custom type declaration
            elif tk_is(-1, txt=("struct", "class", "union", "variant")):
                tk.tk_type = "type"
                if tk.text in custom_types:
                    tk.context = custom_types[tk.text]
                else:
                    tk.context = {"type":tks[idx-1].text, "methods":{}, "members":{}}
                    if tk_is(-1, txt="variant"):
                        tk.context["members"]["type"] = {}
                    custom_types[tk.text] = tk.context

                if tk_is(-2, tk_type="comment"):
                    tk.context["docs"] = tks[idx - 2].text

                after : int = 1
                if tk_is(-1, txt=("struct", "class")) and tk_is(1, txt=":") and (tk_is(2, tk_type="") or tk_is(2, tk_type="type")):
                    tk.context["parent"] = tks[idx+2].text
                    if tk.context["parent"] in custom_types:
                        transfer_dict(tk.context, custom_types[tk.context["parent"]])
                        transfer_dict(tk.context["methods"], custom_types[tk.context["parent"]]["members"])
                        transfer_dict(tk.context["members"], custom_types[tk.context["parent"]]["members"])

                    after = 3
                    custom_types[tk.text] = tk.context

                if tk_is(-1, txt="class") and tk_is(after, txt="virtual"):
                    tk.context["members"]["__VTABLE"] = {"type":"void*", "mods":["protected"]}
                    after += 1

                if tk_is(after, txt="{"):
                    curr_func_type = None
                    curr_type = tk.text
                    brace_count = 0

            # Function declarations / calls
            elif tk_is(1, txt="("):
                tk.tk_type = "function"

                # Declaration
                if (t := tk_get_type(-1)):
                    tk.context = {"type": "".join([x.text for x in t]), "params": ""}

                    # Parameters
                    params : list[Token] = tk_countdown(2)
                    is_param_type : bool = True
                    s : str = ""
                    for p in params:
                        s += p.text
                        if is_param_type and (p.tk_type == "" or p.tk_type == "type"):
                            is_param_type = False
                            s += " "
                        elif p.text == ",":
                            is_param_type = True
                            s += " "
                    tk.context["params"] = s

                    # Modifiers and documentation
                    before : int = tk_find(-1, -1, tk_type="type") - 1
                    if tk_is(before, txt=("public","private","protected","extern","@cfunc")):
                        tk.context["mods"] = [tks[idx + before].text]
                        before -= 1
                    if tk_is(before, txt="virtual"):
                        if "mods" in tk.context:
                            tk.context["mods"].insert(0, "virtual")
                        else:
                            tk.context["mods"] = ["virtual"]
                    if tk_is(before, tk_type="comment"):
                        tk.context["docs"] = tks[idx + before].text

                    if curr_module:
                        modules[curr_module]["funcs"][tk.text] = tk.context
                    elif curr_type:
                        custom_types[curr_type]["methods"][tk.text] = tk.context
                    else:
                        functions[tk.text] = tk.context

                    in_params = True
                    paren_count = 0
                    curr_func_type = tk.context["type"]

                # Method call
                elif tk_is(-1, txt='.'):
                    if tk_is(-2, tk_type="namespace") and tks[idx - 2].context["type"] == "Module":
                        tk.context = modules[tks[idx-2].text]["funcs"].get(tk.text, {})
                    elif tk_is(-2, tk_type="variable") or tk_is(-2, tk_type="parameter"):
                        t : str = extract_type(tks[idx-2].context.get('type', '?'))
                        if t in custom_types:
                            tk.context = custom_types[t]["methods"].get(tk.text, {})

                # Call
                elif tk.text in functions:
                    tk.context = functions[tk.text]

            # Object members
            elif tk_is(-1, txt="."):
                tk.tk_type = "member"
                if tk_is(-2, tk_type="namespace") and tks[idx - 2].context["type"] == "Module":
                    tk.context = modules[tks[idx-2].text]["vars"].get(tk.text, {})
                elif tk_is(-2, tk_type="namespace") and tks[idx - 2].context["type"] == "Enum":
                    tk.context = enums[tks[idx-2].text]["vals"].get(tk.text, {})
                elif tk_is(-2, tk_type="variable") or tk_is(-2, tk_type="parameter") or tk_is(-2, tk_type="member"):
                    t : str = extract_type(tks[idx-2].context.get('type', '?'))
                    if t in custom_types:
                        tk.context = custom_types[t]["members"].get(tk.text, {})

            # @return built-in variable
            elif tk.text == "@return":
                tk.tk_type = "parameter"
                if curr_func_type:
                    if curr_func_type in custom_types:
                        tk.context = {"type": curr_func_type + "*"}
                    else:
                        tk.context = {"type": curr_func_type}
                else:
                    tk.context = {"type": "?"}

            # this built-in variable
            elif tk.text == "this" and curr_type:
                tk.tk_type = "parameter"
                tk.context = {"type": curr_type + "*"}

            # super built-in variable
            elif tk.text == "super" and curr_type and "parent" in custom_types[curr_type]:
                tk.tk_type = "parameter"
                tk.context = {"type": custom_types[curr_type].get("parent", "?") + "*"}

            # Variable declaration
            elif (t := tk_get_type(-1)) is not None:
                tk.context = {"type": "".join([x.text for x in t])}

                if in_params:
                    tk.tk_type = "parameter"
                    tk.context["param"] = True
                else:
                    tk.tk_type = "variable"

                    # Check for modifiers
                    before : int = tk_find(-1, -1, tk_type="type") - 1
                    if tk_is(before, txt="@peek"):
                        tk.context["mods"] = ["@peek"]
                        before -= 1
                    if tk_is(before, txt=("public","private","protected","extern")):
                        if "mods" in tk.context:
                            tk.context["mods"].insert(0, tks[idx + before].text)
                        else:
                            tk.context["mods"] = [tks[idx + before].text]
                        before -= 1

                if tk_is(1, txt="["):
                    tk.context["type"] += "[]"
                tk.modifiers.append(TokenModifier.definition)

                # Module variable
                if not in_params and curr_func_type == None and curr_module:
                    # Check for documentation
                    after : int = tk_find(1, 1, txt=";") + 1
                    if tk_is(after, tk_type="comment") and tks[idx + after].line == tk.line:
                        tk.context["docs"] = tks[idx + after].text
                    elif tk_is(before, tk_type="comment"):
                        tk.context["docs"] = tks[idx + before].text
                    if tk_is(-2, txt="constexpr"):
                        tk.context["mods"] = ["constexpr"]
                        tk.context["val"] = "".join([x.text for x in tk_until(2, txt=";")])

                    modules[curr_module]["vars"][tk.text] = tk.context

                # Object member
                elif not in_params and curr_func_type == None and curr_type:
                    # Change to be member
                    tk.tk_type = "member"
                    if tk_is(1, txt="="):
                        tk.context["default_val"] = "".join([x.text for x in tk_until(2, txt=";")])

                    # Check for documentation
                    after : int = tk_find(1, 1, txt=";") + 1
                    if tk_is(after, tk_type="comment") and tks[idx + after].line == tk.line:
                        tk.context["docs"] = tks[idx + after].text
                    elif tk_is(before, tk_type="comment"):
                        tk.context["docs"] = tks[idx + before].text

                    custom_types[curr_type]["members"][tk.text] = tk.context

                    # Update size and alignment
                    size : int = 0
                    al : int = 0
                    if custom_types[curr_type]["type"] in ("struct", "class"):
                        for _, val in custom_types[curr_type]["members"].items():
                            t : tuple[int, int] = get_type(val.get("type", ""))
                            size = align(size, t[1]) + t[0]
                            al = max(al, t[1])
                    else:
                        size : int = 0
                        al : int = 0
                        for _, val in custom_types[curr_type]["members"].items():
                            t : tuple[int, int] = get_type(val.get("type", ""))
                            size = max(size, t[0])
                            al = max(al, t[1])
                        if custom_types[curr_type]["type"] == "variant":
                            size += al
                    size = align(size, al)
                    custom_types[curr_type]["size"] = size
                    custom_types[curr_type]["align"] = al

                else:
                    # Check for documentation
                    if tk_is(before, tk_type="comment"):
                        tk.context["docs"] = tks[idx + before].text

                    if tk_is(-2, txt="constexpr"):
                        tk.context["mods"] = ["constexpr"]
                        tk.context["val"] = "".join([x.text for x in tk_until(2, txt=";")])

                    variables[tk.text] = tk.context

            # Macro value
            elif tk.text in macros:
                tk.tk_type = "macro"
                tk.context = {"type":"macro", "val": macros[tk.text]}

            # Custom types
            elif tk.text in custom_types:
                tk.tk_type = "type"
                tk.context = custom_types[tk.text]

            # We assume its a variable
            else:
                if tk.text in variables:
                    tk.context = variables[tk.text]
                if tk.context.get('param', False):
                    tk.tk_type = "parameter"
                else:
                    tk.tk_type = "variable"

            idx += 1

        logging.log(logging.CRITICAL, f"Analysed {curr_path} in {time.perf_counter() - start_time} seconds")
        self.cache[curr_path] = (variables, functions, custom_types, modules, enums, macros)

    def parse(self, doc : TextDocument) -> None:
        tks : list[Token] = self.__lex(doc)
        self.__classify_tokens(tks, doc.path, {}, {}, {}, {}, {}, {})
        self.tokens[doc.uri] = tks

    def find_token(self, doc : TextDocument, line : int, offset : int) -> Token | None:
        if not (doc.uri in self.tokens):
            return None
        tks : list[Token] = self.tokens[doc.uri]
        for tk in tks:
            if tk.tk_type == "operator":
                continue
            l : int = max(tk.text.find("\n"), len(tk.text))
            if tk.line == line and tk.offset <= offset <= tk.offset + l:
                return tk
        else:
            return None

server : HolyCowLS = HolyCowLS("hc-lsp", "v0.1")

# ---- Code from reference ----
@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
def did_open(ls: HolyCowLS, params: types.DidOpenTextDocumentParams):
    """"Parse each document when it is opened"""
    doc = ls.workspace.get_text_document(params.text_document.uri)
    ls.parse(doc)

@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls: HolyCowLS, params: types.DidOpenTextDocumentParams):
    """Parse each document when it is changed"""
    doc = ls.workspace.get_text_document(params.text_document.uri)
    ls.parse(doc)

@server.feature(
    types.TEXT_DOCUMENT_SEMANTIC_TOKENS_FULL,
    types.SemanticTokensLegend(
        token_types=TokenTypes,
        token_modifiers=[m.name for m in TokenModifier]
    ),
)
def semantic_tokens_full(ls: HolyCowLS, params: types.SemanticTokensParams):
    """Return the semantic tokens for the entire document"""
    data = []
    tokens = ls.tokens.get(params.text_document.uri, [])

    prev_line : int = 0
    prev_offset : int = 0

    for token in tokens:
        if token.line != prev_line:
            prev_offset = 0
        data.extend(
            [
                token.line - prev_line,
                token.offset - prev_offset,
                len(token.text),
                TokenTypes.index(token.tk_type),
                reduce(operator.or_, token.modifiers, 0)
            ]
        )
        prev_line = token.line
        prev_offset = token.offset

    return types.SemanticTokens(data=data)

@server.feature(types.TEXT_DOCUMENT_HOVER)
def hover(ls: HolyCowLS, params: types.HoverParams):
    pos = params.position
    doc_uri = params.text_document.uri
    doc = ls.workspace.get_text_document(doc_uri)

    tk : Token | None = ls.find_token(doc, pos.line, pos.character)

    return types.Hover(
        contents=types.MarkupContent(
            kind=types.MarkupKind.Markdown,
            value=f"{tk.get_info() if tk else ''}"
        ),
        range=types.Range(
            start=types.Position(line=pos.line, character=0),
            end=types.Position(line=pos.line + 1, character=0),
        ),
    )

# -----------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL, format="%(message)s")
    start_server(server)
