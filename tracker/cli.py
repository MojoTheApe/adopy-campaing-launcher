"""Portable task tracker commands. Run from a repository with Python 3.11+."""
from __future__ import annotations
import argparse
import json
import shutil
import sys
from pathlib import Path
from . import commands, merge, queue
from .github import GitHub, install_labels
from .model import DEFAULT_CONFIG, PRIORITIES, TYPES, load_config, names, payload, tasks_from_issues
from .sync import sync


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--gh-command-json", default='["gh"]')
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sync"); s.add_argument("--output", type=Path, required=True); s.add_argument("--allow-empty", action="store_true")
    sub.add_parser("init", help="Create missing labels and the configured empty release milestone")
    sub.add_parser("list", help="Read current tasks, releases and module descriptions")
    c = sub.add_parser("create")
    c.add_argument("--title", required=True); c.add_argument("--body-file", type=Path)
    c.add_argument("--status", choices=("idea","backlog","todo"), default="idea")
    c.add_argument("--module"); c.add_argument("--priority", choices=PRIORITIES); c.add_argument("--type", dest="kind", choices=TYPES); c.add_argument("--design", action="store_true")
    s = sub.add_parser("set")
    s.add_argument("issue", type=int); s.add_argument("--status", choices=("idea","backlog","todo","review"))
    s.add_argument("--module"); s.add_argument("--priority", choices=PRIORITIES); s.add_argument("--type", dest="kind", choices=TYPES)
    for flag in ("design","blocked","required"): s.add_argument("--" + flag, action=argparse.BooleanOptionalAction, default=None)
    s.add_argument("--reason"); s.add_argument("--milestone", type=int, help="Milestone number; 0 clears the release")
    s = sub.add_parser("start"); s.add_argument("issue", type=int); s.add_argument("--resume", action="store_true", help="Explicitly take over the existing task after inspecting its work and handoff")
    m = sub.add_parser("merged")
    m.add_argument("issues", nargs="+", type=int); m.add_argument("--pr", action="append", type=int, required=True)
    m.add_argument("--apply", action="store_true"); m.add_argument("--reviewed-complete", action="store_true")
    c = sub.add_parser("complete", help="Verified non-code result, with evidence; same final stage")
    c.add_argument("issue", type=int); c.add_argument("--evidence-file", type=Path, required=True); c.add_argument("--verified-non-code", action="store_true", required=True)
    q = sub.add_parser("queue"); q.add_argument("action", choices=("show","begin","next","order","release","cancel")); q.add_argument("issues", nargs="*", type=int)
    m = sub.add_parser("migration-plan"); m.add_argument("--mapping", type=Path, required=True); m.add_argument("--output", type=Path, required=True)
    m = sub.add_parser("migrate"); m.add_argument("--plan", type=Path, required=True); m.add_argument("--rollback", action="store_true")
    e = sub.add_parser("export-template"); e.add_argument("--output", type=Path, required=True); e.add_argument("--repository", required=True); e.add_argument("--name", required=True); e.add_argument("--base", default="main")
    return p


def export(config, args):
    target = args.output.resolve()
    if target.exists(): raise ValueError("Template output must be a new directory")
    project = {"name":args.name,"repository":args.repository,"base_branch":args.base}
    config = dict(config, project=project, completion_label="In " + args.base,
                  modules=[{"id":"product","name":"Product","description":"Replace this module catalog with the new project's boundaries and examples.","examples":[]}])
    # Validate before creating files.
    import re
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repository): raise ValueError("Invalid repository")
    source = Path(__file__).parent
    shutil.copytree(source, target / "tracker", ignore=shutil.ignore_patterns("__pycache__","config.json","*.pyc"))
    (target / "tracker/config.json").write_text(json.dumps(config,indent=2)+"\n",encoding="utf-8")
    (target / "docs").mkdir()
    shutil.copyfile(source / "AGENT_WORKFLOW.md", target / "docs/TRACKER_AGENT_WORKFLOW.md")
    for filename in ("AGENTS.md","CLAUDE.md"):
        (target / filename).write_text("Before task work, read docs/TRACKER_AGENT_WORKFLOW.md and tracker/config.json. Start the selected task with `python -m tracker start ISSUE` before editing code. Follow the repository's review and release rules.\n",encoding="utf-8")
    shutil.copytree(source / "templates", target / ".github")
    return {"template":str(target),"next":"Edit tracker/config.json modules, then run python -m tracker init in the new repository."}


def execute(args, config, api):
    store = queue.StateStore(api, config["project"]["base_branch"])
    if getattr(args,"issue",1) <= 0 or any(n<=0 for n in getattr(args,"issues",[])):
        raise ValueError("Issue numbers must be positive")
    if getattr(args,"module",None) and args.module not in [m["id"] for m in config["modules"]]:
        raise ValueError("Choose a module from tracker/config.json")
    if args.command == "sync": return sync(api,config,args.output,allow_empty=args.allow_empty)
    if args.command == "export-template": return export(config,args)
    if args.command == "init":
        install_labels(api,config)
        milestones = api.request(api.root + "/milestones?state=all&per_page=100",paginate=True)
        title = config.get("default_release")
        if title and not any(m["title"]==title for m in milestones):
            api.request(api.root+"/milestones","POST",{"title":title,"description":"Scope is assigned explicitly by the project owner. An empty milestone is not release-ready."})
        return {"initialized":api.repository}
    if args.command == "list":
        return payload(api.issues(),config,api.request(api.root+"/milestones?state=all&per_page=100",paginate=True),queue.public_state(store.read(optional=True)[0]))
    if args.command == "create":
        result = commands.create(api,config,args)
        if args.status == "todo":
            state,sha=store.read(optional=True);state.setdefault("todo_added",{})[str(result["number"])]=queue.now();store.write(state,sha)
        return {"number":result["number"],"url":result.get("html_url"),"labels":sorted(names(result))}
    if args.command == "set":
        result=commands.change(api,config,store,args.issue,**{k:getattr(args,k) for k in ("status","module","priority","kind","design","blocked","reason","milestone","required")})
        return {"number":result["number"],"labels":sorted(names(result))}
    if args.command == "start": return queue.start(api,config,store,args.issue,args.resume)
    if args.command == "merged":
        if args.apply and not args.reviewed_complete: raise ValueError("Apply requires --reviewed-complete after reviewing ALL task work")
        plan=merge.prepare(api,api.repository,args.issues,args.pr,config["project"]["base_branch"])
        if args.apply:
            merge.apply(api,api.repository,plan)
            state,_=store.read(optional=True)
            if state.get("active") and state["active"]["issue"] in args.issues: queue.release(store,state["active"]["issue"])
        return {"applied":args.apply,"plan":plan}
    if args.command == "complete":
        issue=api.issue(args.issue);labels=names(issue);evidence=args.evidence_file.read_text(encoding="utf-8").strip()
        merge.validate_issue(issue)
        if not evidence or not ({"type:research","type:operations"}&labels):
            raise ValueError("Non-code completion requires Research/Operations and verified evidence")
        if "resolution:non_code" not in labels:
            api.request(api.root+f"/issues/{args.issue}","PATCH",{"body":(issue.get("body") or "")+"\n\n## Verified non-code result\n"+evidence})
        api.labels(args.issue,{"status:merged","resolution:non_code"},{"status:review"})
        state,_=store.read(optional=True)
        if state.get("active") and state["active"]["issue"]==args.issue: queue.release(store,args.issue)
        return {"number":args.issue,"status":"merged","display":"Complete"}
    if args.command == "queue":
        state,sha=store.read(optional=True)
        if args.action=="show": return state
        if args.action=="begin": return queue.begin(store,tasks_from_issues(api.issues(),config))
        if args.action=="next": return queue.next_task(api,config,store)
        if args.action=="order":
            if len(set(args.issues))!=len(args.issues): raise ValueError("Duplicate issue in order")
            for n in args.issues:
                if "status:todo" not in names(api.issue(n)): raise ValueError(f"#{n} is not Todo")
            state["todo_order"]=args.issues;store.write(state,sha);return state
        if args.action=="cancel":
            if state.get("active"): raise ValueError("Release the active task with a handoff first")
            if state.get("run"):state["run"]["state"]="cancelled";store.write(state,sha)
            return state
        if len(args.issues)!=1: raise ValueError("queue release requires one issue number")
        issue=api.issue(args.issues[0]);labels=names(issue)
        if not ({"flag:blocked","status:review","status:merged"}&labels): raise ValueError("Record a blocker or a review handoff before releasing execution")
        queue.release(store,args.issues[0]);return {"released":args.issues[0]}
    if args.command=="migration-plan":
        plan=commands.migration_plan(api,config,json.loads(args.mapping.read_text(encoding="utf-8")))
        args.output.write_text(json.dumps(plan,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
        return {"plan":str(args.output),"issues":len(plan["issues"]),"writes":False}
    if args.command=="migrate": return commands.migrate(api,json.loads(args.plan.read_text(encoding="utf-8")),args.rollback)
    raise ValueError("Unknown command")


def main(argv=None):
    p=parser();args=p.parse_args(argv)
    try:
        config=load_config(args.config)
        command=json.loads(args.gh_command_json)
        if not isinstance(command,list) or not command or any(not isinstance(x,str) or not x for x in command): raise ValueError("Expected a nonempty array of command strings")
        result=execute(args,config,GitHub(config["project"]["repository"],command))
        print(json.dumps(result,indent=2,ensure_ascii=True))
        return 0
    except (OSError,ValueError,RuntimeError,KeyError) as error:
        print(str(error),file=sys.stderr);return 1
