"""Generate unsigned .shortcut files for the spend tracker.

Sign on a Mac before importing:
  shortcuts sign -m anyone -i "Flush Purchases.shortcut" -o "Flush Purchases (signed).shortcut"
"""
import plistlib
import sys
import uuid
from pathlib import Path

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
URL = "https://spend.anilbronson.dev/tx/batch"
TOKEN_PLACEHOLDER = "Bearer PASTE_YOUR_API_TOKEN"
QUEUE = "spend-queue.txt"  # relative to iCloud Drive/Shortcuts/
OBJ = "￼"


def uid():
    return str(uuid.uuid4()).upper()


def text(s):
    return {"Value": {"string": s, "attachmentsByRange": {}}, "WFSerializationType": "WFTextTokenString"}


def out_ref(action_uuid, name, prop=None):
    ref = {"Type": "ActionOutput", "OutputUUID": action_uuid, "OutputName": name}
    if prop:
        ref["Aggrandizements"] = [{"Type": "WFPropertyVariableAggrandizement", "PropertyName": prop}]
    return ref


def attachment(ref):
    return {"Value": ref, "WFSerializationType": "WFTextTokenAttachment"}


def token_string(parts):
    """parts: list of str or ref dicts -> WFTextTokenString."""
    s, attach = "", {}
    for p in parts:
        if isinstance(p, str):
            s += p
        else:
            attach[f"{{{len(s)}, 1}}"] = p
            s += OBJ
    return {"Value": {"string": s, "attachmentsByRange": attach}, "WFSerializationType": "WFTextTokenString"}


def dict_field(items):
    return {
        "Value": {"WFDictionaryFieldValueItems": [
            {"WFItemType": 0, "WFKey": text(k), "WFValue": v} for k, v in items
        ]},
        "WFSerializationType": "WFDictionaryFieldValue",
    }


def act(ident, **params):
    return {"WFWorkflowActionIdentifier": ident, "WFWorkflowActionParameters": params}


def if_start(group, input_ref):
    return act("is.workflow.actions.conditional", GroupingIdentifier=group, WFControlFlowMode=0,
               WFCondition=100,  # "has any value"
               WFInput={"Type": "Variable", "Variable": attachment(input_ref)})


def if_end(group):
    return act("is.workflow.actions.conditional", GroupingIdentifier=group, WFControlFlowMode=2,
               UUID=uid())


def workflow(actions, color, glyph, input_classes=None, types=None):
    return {
        "WFWorkflowClientVersion": "2302.0.4",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": color, "WFWorkflowIconGlyphNumber": glyph},
        "WFWorkflowImportQuestions": [],
        "WFWorkflowInputContentItemClasses": input_classes or [],
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowTypes": types or [],
        "WFWorkflowHasShortcutInputVariables": bool(input_classes),
        "WFQuickActionSurfaces": [],
        "WFWorkflowActions": actions,
    }


# ---------- Flush Purchases ----------
get_file, req, val, txt = uid(), uid(), uid(), uid()
g1, g2 = uid(), uid()
flush = workflow([
    act("is.workflow.actions.documentpicker.open", UUID=get_file,
        WFGetFilePath=text(QUEUE), WFFileErrorIfNotFound=False, WFShowFilePicker=False),
    if_start(g1, out_ref(get_file, "File")),
    act("is.workflow.actions.downloadurl", UUID=req,
        WFURL=URL, WFHTTPMethod="POST", WFHTTPBodyType="JSON", ShowHeaders=True,
        WFHTTPHeaders=dict_field([("Authorization", text(TOKEN_PLACEHOLDER))]),
        WFJSONValues=dict_field([("lines", token_string([out_ref(get_file, "File")]))])),
    act("is.workflow.actions.getvalueforkey", UUID=val,
        WFDictionaryKey="added", WFInput=attachment(out_ref(req, "Contents of URL"))),
    if_start(g2, out_ref(val, "Dictionary Value")),
    act("is.workflow.actions.gettext", UUID=txt, WFTextActionText=text(" ")),
    act("is.workflow.actions.documentpicker.save", UUID=uid(),
        WFInput=attachment(out_ref(txt, "Text")), WFAskWhereToSave=False,
        WFFileDestinationPath=text(QUEUE), WFSaveFileOverwrite=True),
    if_end(g2),
    if_end(g1),
], color=946986751, glyph=61440)

# ---------- Log Purchase ----------
fmt, line = uid(), uid()
inp = lambda prop: {"Type": "ExtensionInput", "Aggrandizements": [
    {"Type": "WFPropertyVariableAggrandizement", "PropertyName": prop}]}
log = workflow([
    act("is.workflow.actions.format.date", UUID=fmt,
        WFDateFormatStyle="ISO 8601", WFISO8601IncludeTime=True,
        WFDate=token_string([{"Type": "CurrentDate"}])),
    act("is.workflow.actions.gettext", UUID=line, WFTextActionText=token_string([
        out_ref(fmt, "Formatted Date"), "|",
        inp("Amount"), "|",
        inp("Card or Pass"), "|",
        inp("Merchant"),
    ])),
    act("is.workflow.actions.file.append", UUID=uid(),
        WFFilePath=text(QUEUE), WFInput=attachment(out_ref(line, "Text")),
        WFAppendOnNewLine=True),
    act("is.workflow.actions.runworkflow", UUID=uid(),
        WFWorkflowName="Flush Purchases",
        WFWorkflow={"workflowName": "Flush Purchases", "isSelf": False},
        WFShowWorkflow=False),
], color=-20702977, glyph=59395,
   input_classes=["WFFinancialTransactionContentItem", "WFStringContentItem", "WFGenericFileContentItem"])

OUT.mkdir(parents=True, exist_ok=True)
for name, wf in [("Flush Purchases", flush), ("Log Purchase", log)]:
    with open(OUT / f"{name}.shortcut", "wb") as f:
        plistlib.dump(wf, f, fmt=plistlib.FMT_BINARY)
    print("wrote", OUT / f"{name}.shortcut")
