"""Switching order for terminals and agents.

Three separate questions, kept separate on purpose (tmux is right about this):
a stable index for jumping straight to one, most-recently-used for toggling
back, and the ownership tree for looking at the set. Only the index is stored.
"""
import unittest
from unittest import mock

import agent_loop


class _Item:
    """Stand-in for a TerminalInfo/AgentInfo with just the fields order uses."""

    def __init__(self, key, index, parent=None):
        self.key = key
        self.index = index
        self.parent = parent


def _rows(items):
    return agent_loop._tree_rows(
        items, key_of=lambda i: i.key, parent_of=lambda i: i.parent)


class NextIndexTests(unittest.TestCase):
    def test_first_index_is_one(self):
        # Zero belongs to term0 / the primary agent, which never asks for one.
        self.assertEqual(1, agent_loop._next_switch_index([]))

    def test_it_fills_the_lowest_free_slot(self):
        existing = [_Item("a", 0), _Item("b", 1), _Item("c", 3)]
        self.assertEqual(2, agent_loop._next_switch_index(existing))

    def test_a_closed_entry_frees_its_number(self):
        """Reusing a closed slot keeps the numbers small and typeable, which is
        the only reason to have numbers at all. What must never happen is a
        LIVE entry changing number — and filling a gap cannot do that."""
        alive = [_Item("a", 0), _Item("c", 2)]
        self.assertEqual(1, agent_loop._next_switch_index(alive))

    def test_a_missing_or_unparseable_index_is_ignored(self):
        broken = [_Item("a", None), _Item("b", "x"), _Item("c", 1)]
        self.assertEqual(2, agent_loop._next_switch_index(broken))


class TreeRowTests(unittest.TestCase):
    def test_children_follow_their_parent_and_are_indented(self):
        items = [_Item("term0", 0), _Item("b", 1, "term0"),
                 _Item("c", 2, "b"), _Item("d", 3, "term0")]
        self.assertEqual(
            [(0, "term0"), (1, "b"), (2, "c"), (1, "d")],
            [(depth, item.key) for depth, item in _rows(items)])

    def test_siblings_keep_switch_index_order(self):
        # The tree is a way of reading the ring, not a second ordering to
        # reconcile with it.
        items = [_Item("term0", 0), _Item("late", 5, "term0"),
                 _Item("early", 2, "term0")]
        self.assertEqual(
            ["term0", "early", "late"],
            [item.key for _, item in _rows(items)])

    def test_an_orphan_is_shown_at_the_root_not_dropped(self):
        # A terminal you cannot switch to is worse than an oddly indented one.
        items = [_Item("term0", 0), _Item("orphan", 1, "vanished")]
        keys = [item.key for _, item in _rows(items)]
        self.assertIn("orphan", keys)
        self.assertEqual(0, dict(
            (item.key, depth) for depth, item in _rows(items))["orphan"])

    def test_a_parent_cycle_still_lists_everyone_once(self):
        items = [_Item("a", 1, "b"), _Item("b", 2, "a")]
        keys = [item.key for _, item in _rows(items)]
        self.assertEqual(sorted(keys), ["a", "b"])

    def test_self_parenting_is_treated_as_a_root(self):
        items = [_Item("a", 1, "a")]
        self.assertEqual([(0, "a")],
                         [(d, i.key) for d, i in _rows(items)])

    def test_an_empty_collection_is_empty(self):
        self.assertEqual([], _rows([]))


class RegistryOrderTests(unittest.TestCase):
    """The real registries, exercised through their public order functions."""

    def setUp(self):
        self._terminals = dict(agent_loop._terminal_registry)
        self._agents = dict(agent_loop._agent_registry)
        agent_loop._terminal_registry.clear()
        agent_loop._agent_registry.clear()

    def tearDown(self):
        agent_loop._terminal_registry.clear()
        agent_loop._terminal_registry.update(self._terminals)
        agent_loop._agent_registry.clear()
        agent_loop._agent_registry.update(self._agents)

    def _terminal(self, name, index, parent=None):
        info = agent_loop.TerminalInfo(
            name=name, command="bash", session=mock.Mock(),
            created_at=0.0, created_by="test", index=index,
            parent_terminal=parent)
        agent_loop._terminal_registry[name] = info
        return info

    def _agent(self, agent_id, index, parent=None):
        info = agent_loop.AgentInfo(
            id=agent_id, name=agent_id, index=index, parent_id=parent)
        agent_loop._agent_registry[agent_id] = info
        return info

    def test_terminals_come_back_in_index_order(self):
        self._terminal("z", 3)
        self._terminal("term0", 0)
        self._terminal("a", 1)
        self.assertEqual(["term0", "a", "z"],
                         [t.name for t in agent_loop.ordered_terminals()])

    def test_agents_come_back_in_index_order(self):
        self._agent("AI-9", 2)
        self._agent("primary", 0)
        self.assertEqual(["primary", "AI-9"],
                         [a.id for a in agent_loop.ordered_agents()])

    def test_equal_indices_fall_back_to_the_name(self):
        # Two entries can only share an index through a restored/persisted
        # state; the order still has to be deterministic.
        self._agent("b", 1)
        self._agent("a", 1)
        self.assertEqual(["a", "b"],
                         [a.id for a in agent_loop.ordered_agents()])

    def test_the_terminal_tree_follows_parent_links(self):
        self._terminal("term0", 0)
        self._terminal("build", 1, parent="term0")
        self._terminal("inner", 2, parent="build")
        self.assertEqual(
            [(0, "term0"), (1, "build"), (2, "inner")],
            [(d, t.name) for d, t in agent_loop.terminal_tree_rows()])

    def test_the_agent_tree_follows_parent_links(self):
        self._agent("primary", 0)
        self._agent("AI-2", 1, parent="primary")
        self._agent("AI-3", 2, parent="AI-2")
        self.assertEqual(
            [(0, "primary"), (1, "AI-2"), (2, "AI-3")],
            [(d, a.id) for d, a in agent_loop.agent_tree_rows()])


class RegistrationTests(unittest.TestCase):
    """Indices are handed out where the entries are created."""

    def setUp(self):
        self._terminals = dict(agent_loop._terminal_registry)
        self._agents = dict(agent_loop._agent_registry)
        agent_loop._terminal_registry.clear()
        agent_loop._agent_registry.clear()

    def tearDown(self):
        agent_loop._terminal_registry.clear()
        agent_loop._terminal_registry.update(self._terminals)
        agent_loop._agent_registry.clear()
        agent_loop._agent_registry.update(self._agents)

    def _session(self):
        session = mock.Mock()
        session.is_alive.return_value = True
        return session

    def test_term0_is_always_zero_and_children_count_up(self):
        agent_loop.register_terminal(self._session(), "bash", 0, name="term0")
        agent_loop.register_terminal(self._session(), "bash", 0, name="build")
        agent_loop.register_terminal(self._session(), "bash", 0, name="docs")
        by_name = {t.name: t.index for t in agent_loop.ordered_terminals()}
        self.assertEqual({"term0": 0, "build": 1, "docs": 2}, by_name)

    def test_a_closed_terminal_releases_its_number(self):
        agent_loop.register_terminal(self._session(), "bash", 0, name="term0")
        agent_loop.register_terminal(self._session(), "bash", 0, name="build")
        agent_loop.register_terminal(self._session(), "bash", 0, name="docs")
        agent_loop.unregister_terminal("build")
        agent_loop.register_terminal(self._session(), "bash", 0, name="again")
        self.assertEqual(1, agent_loop.get_terminal("again").index)
        # And the survivor kept its own.
        self.assertEqual(2, agent_loop.get_terminal("docs").index)

    def test_the_primary_agent_is_zero(self):
        agent_loop.register_agent("primary", role="primary")
        agent_loop.register_agent("AI-2")
        by_id = {a.id: a.index for a in agent_loop.ordered_agents()}
        self.assertEqual(0, by_id["primary"])
        self.assertEqual(1, by_id["AI-2"])


if __name__ == "__main__":
    unittest.main()
