"""Resolve immutable correction history, never display hints or save timestamps.

The caller supplies an unbounded inventory and all stored check revisions from
one database snapshot. Missing or inconsistent evidence is not a current copy.
"""
import re

HASH = re.compile(r'[0-9a-f]{64}')

class WorkingCopies:
    def __init__(self, bases, histories):
        self.items = {}; self.parents = {}; self.children = {}; self.invalid = set()
        for key, base in bases.items():
            records = histories.get(key, [])
            self.items[key] = records[-1] if records else base
            self.children[key] = set()
            if not records:
                self.invalid.add(key)
            parent = None
            outgoing = set()
            for record in records:
                link = record.get('fixed_from')
                if parent is not None and link != parent:
                    self.invalid.add(key)
                if link:
                    if not isinstance(link, dict) or not isinstance(link.get('file_hash'), str) or not HASH.fullmatch(link['file_hash']):
                        self.invalid.add(key)
                    else:
                        parent = link
                fixes = record.get('fixes_applied') or []
                if not isinstance(fixes, list):
                    self.invalid.add(key); continue
                current = set()
                for fix in fixes:
                    child = fix.get('fixed_file_hash') if isinstance(fix, dict) else None
                    if not isinstance(child, str) or not HASH.fullmatch(child):
                        self.invalid.add(key)
                    else:
                        current.add(child)
                if not outgoing.issubset(current):
                    self.invalid.add(key)
                outgoing |= current
            self.parents[key] = parent.get('file_hash') if parent else None
            self.children[key] = outgoing
        for child, parent in self.parents.items():
            if parent is not None:
                if parent not in self.items:
                    self.invalid.add(child)
                elif child not in self.children[parent]:
                    self.invalid.update((parent, child))
                    self.children[parent].add(child)
        for parent, children in self.children.items():
            for child in children:
                if child not in self.items or self.parents.get(child) != parent:
                    self.invalid.add(parent)
                    if child in self.items:
                        self.invalid.add(child)

    def resolve(self, key, authorize=lambda item: True):
        out = dict(requested_hash=key, status='unavailable', working_hash=None, choices=[], history=[], read_only_reason=None)
        if key not in self.items or not authorize(self.items[key]):
            return out
        if self.parents.get(key) == key:
            out['unavailable_reason'] = 'history_inconsistent'
            out['message'] = 'This copy has inconsistent saved version history and is read-only. Its original download is unchanged. Ask an admin to review the version history.'
            return out
        ancestors = []; seen = set(); current = key
        while current is not None:
            if current in seen or current not in self.items or current in self.invalid:
                return out
            seen.add(current); ancestors.append(current); current = self.parents[current]
        leaves = []; visiting = set(); done = set(); stack = [(key, False)]
        while stack:
            node, closing = stack.pop()
            if closing:
                visiting.remove(node); done.add(node); continue
            if node in visiting or node not in self.items or node in self.invalid:
                return out
            if node in done:
                continue
            visiting.add(node); stack.append((node, True))
            children = self.children[node]
            if not children:
                leaves.append(node)
            stack.extend((child, False) for child in sorted(children, reverse=True))
        # Never identify or count inaccessible branches, or route through a hidden parent.
        if not all(authorize(self.items[node]) for node in seen | done):
            return out
        def reference(node):
            return dict(file_hash=node, filename=self.items[node].get('filename') or 'Saved report')
        if not leaves:
            return out
        chosen = leaves[0] if len(leaves) == 1 else None
        out.update(status=('choice' if chosen is None else 'current' if chosen == key else 'superseded'),
                   working_hash=chosen, choices=[reference(node) for node in leaves],
                   history=[reference(node) for node in ancestors[1:]])
        if chosen:
            item = self.items[chosen]
            out['read_only_reason'] = ('sent' if any(x.get('sent') for x in item.get('sent') or []) else
                                      'sending' if item.get('send_pending') or item.get('send_in_progress') else
                                      'approved' if item.get('approval') else None)
        return out
