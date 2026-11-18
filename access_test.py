"""Регрессии заявок: подача, дубли, повтор, уже приглашённый и уже участник, метка от чужого, без токена."""
import unittest
import access as a


class Fake:
    def __init__(self, issues=(), collaborators=(), invited=()):
        self.issues, self.collaborators, self.invited, self.writes = list(issues), set(collaborators), set(invited), []

    def __call__(self, method, path, token, body=None):
        if method == 'GET' and '/issues?' in path:
            return 200, [{'number': n} for n in self.issues]
        if method == 'GET' and '/collaborators/' in path:
            return (204 if path.rsplit('/', 1)[1] in self.collaborators else 404), None
        if method == 'GET' and path.endswith('/invitations?per_page=100'):
            return 200, [{'invitee': {'login': l}} for l in self.invited]
        self.writes.append((method, path, body))
        if method == 'PUT':
            self.invited.add(path.rsplit('/', 1)[1])
            return 201, {}
        return 200, {}


def flow(fake, action, number=5, login='newbie', label='', sender='newbie', token='pat', dry=False):
    s = a.state(login, number, 'gh', token, api=fake)
    plan = a.decide(action, login, label, sender, s)
    a.apply(number, login, plan, 'gh', token, dry, api=fake)
    return plan


class Access(unittest.TestCase):
    def setUp(self):
        a.ACCESS, a.TARGET, a.LABEL, a.APPROVERS = 'aust917/polden-rnd-access', 'aust917/polden-rnd', 'одобрено', {'aust917'}

    def test_new_request_waits_then_approval_invites_and_closes(self):
        fake = Fake(issues=[5])
        plan = flow(fake, 'opened')
        self.assertIn('ждёт одобрения', plan['comment'])
        self.assertIsNone(plan['close'])
        plan = flow(fake, 'labeled', label='одобрено', sender='aust917')
        self.assertTrue(plan['invite'])
        self.assertEqual([w[0] for w in fake.writes], ['POST', 'PUT', 'POST', 'PATCH'])
        self.assertEqual(fake.writes[1][2], {'permission': 'push'})
        self.assertEqual(fake.writes[-1][2], {'state': 'closed', 'state_reason': 'completed'})

    def test_duplicate_open_request_closed(self):
        fake = Fake(issues=[3, 5])
        plan = flow(fake, 'opened', number=5)
        self.assertEqual(plan['close'], 'not_planned')
        self.assertIn('#3', plan['comment'])
        self.assertEqual(flow(Fake(issues=[3, 5]), 'opened', number=3)['close'], None, 'первая заявка — не дубль')

    def test_repeat_after_invite_and_existing_member(self):
        plan = flow(Fake(invited=['newbie']), 'opened')
        self.assertEqual(plan['close'], 'completed')
        self.assertIn('уже отправлено', plan['comment'])
        plan = flow(Fake(collaborators=['newbie']), 'reopened')
        self.assertIn('уже есть доступ', plan['comment'])
        fake = Fake(invited=['newbie'])
        plan = flow(fake, 'labeled', label='одобрено', sender='aust917')
        self.assertFalse(plan['invite'], 'повторно не приглашаем')
        self.assertNotIn('PUT', [w[0] for w in fake.writes])

    def test_label_from_stranger_or_other_label_ignored(self):
        fake = Fake()
        self.assertFalse(flow(fake, 'labeled', label='одобрено', sender='newbie')['invite'])
        self.assertFalse(flow(fake, 'labeled', label='вопрос', sender='aust917')['invite'])
        self.assertNotIn('PUT', [w[0] for w in fake.writes])

    def test_without_token_fails_loudly_without_invite(self):
        fake = Fake()
        plan = flow(fake, 'labeled', label='одобрено', sender='aust917', token='')
        self.assertTrue(plan['fail'])
        self.assertNotIn('PUT', [w[0] for w in fake.writes])
        self.assertIsNone(flow(Fake(), 'opened', token='')['close'], 'подача без токена всё равно принимается')

    def test_dry_run_writes_nothing(self):
        fake = Fake()
        a.apply(5, 'newbie', a.decide('labeled', 'newbie', 'одобрено', 'aust917',
                                      a.state('newbie', 5, 'gh', 'pat', api=fake)), 'gh', 'pat', True, api=fake)
        self.assertEqual(fake.writes, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
