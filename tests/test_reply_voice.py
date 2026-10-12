import unittest

from app.reply_voice import closed_offer_acknowledgement


class ClosedOfferTests(unittest.TestCase):
    closing = 'Hvala na odgovoru. Ako kasnije zatreba sajt, imate 50% popusta na izradu.'

    def context(self, incoming, outgoing=None, message_type=1):
        return {'messages': [{'direction': 'OUTGOING', 'body': outgoing or self.closing},
                             {'direction': 'INCOMING', 'body': incoming, 'message_type': message_type}]}

    def test_closing_acknowledgements_are_silent(self):
        for value in ('👍', '👍🏽', '👍\ufe0f', 'Hvala!', 'Važi, hvala.', 'Ne,hvala', 'OK'):
            with self.subTest(value=value):
                self.assertTrue(closed_offer_acknowledgement(self.context(value)))

    def test_real_enquiries_and_consent_are_not_suppressed(self):
        for value in ('Da', 'Ipak može', 'Koliko bi bilo?', 'Mogu li popust preneti prijatelju?',
                      'Hvala, možete li mi poslati primer?', 'Avans je uplaćen', ''):
            with self.subTest(value=value):
                self.assertFalse(closed_offer_acknowledgement(self.context(value)))
        self.assertFalse(closed_offer_acknowledgement(self.context('👍', message_type=2)))
        self.assertFalse(closed_offer_acknowledgement(self.context('👍', outgoing='Imate 50% popusta. Možemo da počnemo?')))
        self.assertFalse(closed_offer_acknowledgement(self.context('Hvala', outgoing='Hosting je uključen.')))

    def test_message_burst_with_new_question_reopens(self):
        context = self.context('👍')
        context['messages'].append({'direction': 'INCOMING', 'body': 'Koliko bi trajala izrada?'})
        self.assertFalse(closed_offer_acknowledgement(context))
        context['messages'][-1]['body'] = 'Hvala'
        self.assertTrue(closed_offer_acknowledgement(context))

