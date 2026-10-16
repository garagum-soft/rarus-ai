"""Accounts mode: one server, several people, each with an engine of their own.

Off unless someone turns it on with ``raven accounts serve``; a Raven started
any other way never reads anything here, and nothing here changes how it runs.

When it is on, the account gateway (:mod:`raven.accounts.hub`) is what people's
browsers talk to. It signs them in (a password, or Google Sign-In once the
administrator has entered the keys), and puts each one through to that person's
own engine: an ordinary ``raven gateway`` with a home of its own -- its own
keys and subscriptions, conversations, agents and messengers -- started and
watched by :mod:`raven.accounts.engines`. Model calls are priced in credits by
the administrator's table (:mod:`raven.accounts.billing`), read off each
engine's usage files; an account at zero, or over its disk allowance, starts no
new work until it is topped up (:mod:`raven.accounts.gate`), while work already
running finishes.

State lives under ``<RAVEN_HOME>/accounts`` of the server's own home
(:mod:`raven.accounts.paths`).
"""
