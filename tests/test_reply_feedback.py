from contextlib import closing
import json
import threading
import unittest

from app.codex_replies import validate_result
from app.portfolio import candidates, catalog, normalize
from app.reply_feedback import ReplyFeedback
from app.storage import connect
import test_controller


ASSUMPTION = {'topic': 'maintenance_sixth_change',
              'assumption': 'Za šestu izmenu predlažem zasebnu procenu obima.',
              'question': 'Kako da ubuduće ponudim šestu izmenu u mesecu?'}


class PortfolioTests(unittest.TestCase):
    def test_catalog_counts_and_hair_examples(self):
        self.assertEqual(len(catalog()), 400)
        context = {'contact': {'business_name': 'Frizerski salon', 'viber_name': 'Person'},
                   'messages': [{'direction': 'INCOMING', 'body': 'Možete li poslati primer rada?'}]}
        rows = candidates(context)
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r['category'] == 'Hair salon' for r in rows))
        self.assertEqual(rows[0]['url'], 'https://bibbis.rs/')
        self.assertNotIn('search_terms', rows[0])
        self.assertLess(len(json.dumps(rows)), 3500)

    def test_invalid_urls_are_rejected_and_unknown_category_is_empty(self):
        for url in ('javascript:alert(1)', 'file:///private', 'https://user:secret@site.test'):
            with self.assertRaises(ValueError):
                normalize([{'category': 'Test', 'serbian': [{'name': 'Test', 'url': url}]}])
        self.assertEqual(candidates({'contact': {'business_name': 'XYZ'}, 'messages': []}), [])

    def test_structured_assumptions_validation(self):
        valid = {'action': 'reply', 'text': 'U redu.', 'reason': 'Proposal', 'assumptions': [ASSUMPTION]}
        self.assertEqual(validate_result(valid), valid)
        for value in (None, {}, [ASSUMPTION] * 6,
                      [{**ASSUMPTION, 'topic': 'arbitrary instructions'}],
                      [{**ASSUMPTION, 'question': ''}], [{**ASSUMPTION, 'extra': 'bad'}]):
            with self.assertRaises(ValueError): validate_result({**valid, 'assumptions': value})


class FeedbackTests(unittest.TestCase):
    # Reuse the managed fake Viber fixture; there is never a real send here.
    setUp = test_controller.DraftTests.setUp
    tearDown = test_controller.DraftTests.tearDown
    incoming = test_controller.DraftTests.incoming
    wait_state = test_controller.DraftTests.wait_state
    def assumed_draft(self):
        self.generator.result = {'action': 'reply', 'text': 'Mogu da predložim obim za dodatnu izmenu.',
                                 'reason': 'Conditional proposal', 'assumptions': [ASSUMPTION]}
        self.incoming(); self.replies.tick()
        return self.wait_state('DRAFT')

    def test_assumption_does_not_block_draft_approval(self):
        draft = self.assumed_draft()
        self.assertEqual(draft['assumptions'], [ASSUMPTION])
        question = self.replies.feedback.questions()[0]
        self.assertEqual(question['state'], 'OPEN')
        self.assertEqual(self.desktop.sent, [])
        self.replies.review_draft(draft['id'], True)
        self.wait_state('DISPATCHED')
        self.assertEqual(len(self.desktop.sent), 1)
        self.assertEqual(self.replies.feedback.questions()[0]['state'], 'OPEN')

    def test_answer_is_durable_trusted_rule_and_invalidates_stale_draft(self):
        draft = self.assumed_draft()
        question = self.replies.feedback.questions()[0]
        before = self.replies.settings()['revision']
        answer = 'Šestu malu izmenu uradi besplatno; veći obim proceni posebno.'
        self.replies.feedback.answer(question['id'], answer)
        self.assertEqual(self.replies.settings()['revision'], before + 1)
        self.assertEqual(self.replies.jobs()[0]['state'], 'STALE')
        with self.assertRaises(ValueError): self.replies.review_draft(draft['id'], True)
        reopened = ReplyFeedback(self.replies)
        self.assertEqual(reopened.questions()[0]['answer'], answer)
        recorded = []
        original = self.generator.generate
        def generate(context, instructions):
            recorded.append(instructions)
            return original(context, instructions)
        self.generator.generate = generate
        self.generator.result = {'action': 'reply', 'text': 'Šestu malu izmenu mogu da uradim besplatno.', 'reason': 'Saved rule', 'assumptions': []}
        self.replies.regenerate(draft['id'])
        self.wait_state('DRAFT')
        self.assertEqual(self.generator.contexts[-1]['saved_owner_rules'][0]['owner_rule'], answer)
        self.assertNotIn(answer, recorded[0])
        self.assertEqual(len(reopened.questions()), 1)
        self.assertEqual(self.desktop.sent, [])
        revision = self.replies.settings()['revision']
        reopened.answer(question['id'], answer)
        self.assertEqual(self.replies.settings()['revision'], revision)

    def test_duplicate_questions_are_atomic_and_account_scoped(self):
        draft = self.assumed_draft()
        job = self.replies._job(draft['id'])
        failures = []
        def insert():
            try:
                with closing(connect(self.path)) as db, db:
                    self.replies.feedback.record(db, job, [ASSUMPTION])
            except Exception as e: failures.append(e)
        threads = [threading.Thread(target=insert) for _ in range(5)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(len(self.replies.feedback.questions()), 1)
        question_id = self.replies.feedback.questions()[0]['id']
        with closing(connect(self.path)) as db, db:
            db.execute("UPDATE reply_owner_questions SET account_id='other'")
        self.assertEqual(self.replies.feedback.questions(), [])
        with self.assertRaises(ValueError): self.replies.feedback.answer(question_id, 'No access')

    def test_regenerate_never_replays_newer_message_or_uncertain_send(self):
        draft = self.assumed_draft()
        question = self.replies.feedback.questions()[0]
        self.replies.feedback.answer(question['id'], 'Proceni obim posebno.')
        self.incoming(3)
        with self.assertRaises(ValueError): self.replies.regenerate(draft['id'])
        self.assertEqual(self.desktop.sent, [])

    def test_dashboard_question_endpoint_and_csrf(self):
        from fastapi.testclient import TestClient
        from app.controller import create_app
        self.assumed_draft()
        client = TestClient(create_app(self.service), base_url='http://127.0.0.1:4001')
        session = client.post('/viber/api/session', json={}, headers={'Origin': 'http://127.0.0.1:4001', 'X-Viber-Browser': '1'})
        headers = {'X-Viber-CSRF': session.json()['csrf'], 'Origin': 'http://127.0.0.1:4001'}
        questions = client.get('/viber/api/replies', headers=headers).json()['owner_questions']
        payload = {'question_id': questions[0]['id'], 'answer': 'Šesta mala izmena je besplatna.'}
        self.assertEqual(client.post('/viber/api/replies/answer', json=payload).status_code, 403)
        self.assertEqual(client.post('/viber/api/replies/answer', json=payload, headers=headers).status_code, 200)
        self.assertEqual(client.get('/viber/api/replies', headers=headers).json()['owner_questions'][0]['state'], 'ANSWERED')


if __name__ == '__main__': unittest.main()
