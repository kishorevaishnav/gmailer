# Exact-subject "never store" uses Gmail trash plus a rule

The "Don't repeat this subject" action trashes the current email from Gmail,
purges every cached copy of that exact sender+subject (case-insensitive), and
creates a trash rule matching sender + exact subject so future identical mail is
removed from Gmail on queue load and never cached locally.

Every delete in the app goes to Gmail trash (30-day recovery), never permanent
deletion — a deliberate, whole-app choice. So this action is implemented by
short-circuiting the message before it can be stored, rather than by a permanent
Gmail delete. We match on sender + exact subject (not subject alone) because a
bare subject match would silently trash legitimate mail from other senders.