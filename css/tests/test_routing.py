"""Forwarding shim: the router tests live with the llmfleet package.

Keeps ``pytest css/tests`` exercising the router (css imports it through the
css.model.routing shim, so package regressions are css regressions too).
"""
from llmfleet.tests.test_routing import *  # noqa: F401,F403
