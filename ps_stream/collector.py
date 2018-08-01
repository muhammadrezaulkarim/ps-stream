import logging
import pytz
from datetime import datetime
from xml.etree import ElementTree

import ujson as json
from confluent_kafka import Producer
from twisted.internet import endpoints, reactor
from twisted.web import resource, server

from .utils import element_to_obj

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)


class PSStreamCollector(resource.Resource):

    isLeaf = True

    def __init__(self, producer, topic=None, authorize_f=None):
        super().__init__()
        self.producer = producer
        self.topic = topic
        self.authorize_f = authorize_f

    def render_GET(self, request):
        return '{"status":"GET ok"}'.encode('utf-8')

    def render_POST(self, request):
        """Decode PeopleSoft rowset-based messages into transactions, and produce Kafka
        messages for each transaction. PeopleSoft is expected to POST messages as events
        occur via SYNC and FULLSYNC services.

        The following URL describes the PeopleSoft Rowset-Based Message Format.
        http://docs.oracle.com/cd/E66686_01/pt855pbr1/eng/pt/tibr/concept_PeopleSoftRowset-BasedMessageFormat-0764fb.html
        """
        log.debug('To: {}, From: {}, MessageName: {}'.format(
                request.getHeader('To'),
                request.getHeader('From'),
                request.getHeader('MessageName')))

        if self.authorize_f and not self.authorize_f(request):
            request.setResponseCode(403, message='Forbidden')
            log.info('Unauthorized message received')
            log.debug('To: {}, From: {}, MessageName: {}'.format(
                request.getHeader('To'),
                request.getHeader('From'),
                request.getHeader('MessageName')))
            return 'Message not accepted by collector.'.encode('utf-8')

        assert(request.getHeader('DataChunk') == '1')
        assert(request.getHeader('DataChunkCount') == '1')
        
        psft_message_name = None
        field_types = None

        transaction_id = request.getHeader('TransactionID')
        orig_time_stamp = request.getHeader('OrigTimeStamp')

        # Parse the root element for the PeopleSoft message name and FieldTypes
        request.content.seek(0, 0)
        for event, e in ElementTree.iterparse(request.content, events=('start', 'end')):
            if event == 'start' and psft_message_name is None:
                psft_message_name = e.tag.split('}', 1)[-1]
            elif event == 'end' and e.tag.split('}', 1)[-1] == 'FieldTypes':
                field_types = element_to_obj(e, value_f=field_type)
                break

        # Rescan for transactions, removing read elements to reduce memory usage
        transaction_index = 1
        request.content.seek(0, 0)
        for event, e in ElementTree.iterparse(request.content, events=('end',)):
            if e.tag.split('}', 1)[-1] == 'Transaction':
                transaction = ElementTree.tostring(e, encoding='unicode')
                message = {
                    'TransactionID': transaction_id,
                    'TransactionIndex': transaction_index,
                    'OrigTimeStamp': orig_time_stamp,
                    'CollectTimeStamp': datetime.now(pytz.utc).astimezone().isoformat(),
                    'Transaction': transaction
                }
                message_str = json.dumps(message)
                self.producer.produce(self.topic, message_str, transaction_id)
                e.clear()
                transaction_index += 1
        self.producer.flush()

        return '{"status":"POST ok"}'.encode('utf-8')


def collect(config, topic=None, port=8000, senders=None, recipients=None, message_names=None):
    def authorize_request(request):
        if senders and not request.getHeader('To') in senders:
            return False
        if recipients and not request.getHeader('From') in senders:
            return False
        if message_names and request.getHeader('MessageName') in senders:
            return False
        return True

    producer = Producer(config)
    collector = PSStreamCollector(producer, topic=topic, authorize_f=authorize_request)
    site = server.Site(collector)
    endpoint = endpoints.TCP4ServerEndpoint(reactor, int(port))
    endpoint.listen(site)
    log.info(f'Listening for connections on port {port}')
    reactor.run()


def field_type(element):
    assert('type' in element.attrib)
    return element.attrib.get('type')
