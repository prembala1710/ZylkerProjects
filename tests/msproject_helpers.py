"""Shared by the Microsoft Project tests: small builders for MSPDI documents and a ready-made project."""

import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from helpers import ApiTestCase

NS = "http://schemas.microsoft.com/project"


def el(tag, value):
    return "<%s>%s</%s>" % (tag, escape(str(value)), tag)


def task(uid, name=None, level=1, links=(), **fields):
    """A ``<Task>`` with only the elements given. A link is ``(predecessor uid, type, lag in tenths of a minute, lag format,
    cross-project flag)``; only the first is required (finish-to-start, no lag)."""
    parts = [el("UID", uid), el("ID", uid)]
    if name is not None:
        parts.append(el("Name", name))
    parts.append(el("OutlineLevel", level))
    parts += [el(key, value) for key, value in fields.items()]
    for link in links:
        link = tuple(link) if isinstance(link, (tuple, list)) else (link,)
        pred, kind, lag, fmt, cross = link + (None, 1, 0, 7, 0)[len(link) :]
        parts.append("<PredecessorLink>%s</PredecessorLink>" % "".join([el("PredecessorUID", pred), el("Type", kind), el("CrossProject", cross), el("LinkLag", lag), el("LagFormat", fmt)]))
    return "<Task>%s</Task>" % "".join(parts)


def resource(uid, name, **fields):
    return "<Resource>%s</Resource>" % "".join([el("UID", uid), el("Name", name)] + [el(k, v) for k, v in fields.items()])


def assignment(task_uid, resource_uid, work=None):
    return "<Assignment>%s</Assignment>" % "".join([el("TaskUID", task_uid), el("ResourceUID", resource_uid)] + ([el("Work", work)] if work else []))


def doc(*tasks, resources=(), assignments=(), **project):
    """A complete document; ``project`` holds the project-level elements (``UID``, ``Name``, ``Author``...)."""
    head = "".join(el(key, value) for key, value in project.items())
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Project xmlns="%s">%s<Tasks>%s</Tasks><Resources>%s</Resources><Assignments>%s</Assignments></Project>'
        % (NS, head, "".join(tasks), "".join(resources), "".join(assignments))
    ).encode("utf-8")


def local(tag):
    return tag.rpartition("}")[2]


def child(element, name):
    return next((c for c in element if local(c.tag) == name), None)


def text(element, name, default=None):
    found = child(element, name)
    return default if found is None else (found.text or "")


class MspCase(ApiTestCase):
    """Project IMP with two members, ready to import into."""

    def setUp(self):
        super().setUp()
        self.make_project("IMP", members=("bob", "carol"))
        self.alice = self.client("alice")

    def post_xml(self, data, key="IMP", user="alice", content_type="application/xml", **query):
        return self.client(user).request("POST", "/api/projects/%s/import/msproject" % key, raw=data, content_type=content_type, query=query)

    def run_import(self, data, key="IMP", **query):
        return self.ok(self.post_xml(data, key, **query))

    def tasks(self, key="IMP"):
        return self.ok(self.alice.get("/api/tasks", query={"project": key, "limit": 200, "sort": "number"}))["items"]

    def by_title(self, key="IMP"):
        return {t["title"]: t for t in self.tasks(key)}

    def milestones(self, key="IMP"):
        return self.ok(self.alice.get("/api/projects/%s/milestones" % key))["items"]

    def blockers(self, task_key):
        detail = self.ok(self.alice.get("/api/tasks/" + task_key))
        return sorted((d["key"], d["type"], d["lag_days"]) for d in detail["blocked_by"])

    def export(self, key="IMP", user="alice"):
        return self.client(user).get("/api/projects/%s/export/msproject" % key)

    def exported(self, key="IMP"):
        response = self.export(key)
        self.assertEqual(response.status, 200, response.text)
        return ET.fromstring(response.body)

    def counts(self, key="IMP"):
        """What an import could change, to prove that a refused or dry-run import changed none of it."""
        conn_sql = "SELECT (SELECT COUNT(*) FROM tasks), (SELECT COUNT(*) FROM milestones), (SELECT COUNT(*) FROM task_dependencies), (SELECT COUNT(*) FROM project_members), (SELECT COUNT(*) FROM activity), (SELECT next_task_number FROM projects WHERE key = '%s')" % key
        from zylker_pm.db import connect

        conn = connect(self.config.db_path)
        try:
            return tuple(conn.execute(conn_sql).fetchone())
        finally:
            conn.close()
