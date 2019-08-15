"""
Thus module contains wrappers for different types of Bloomberg requests
"""
import asyncio
import datetime as dt
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple
from typing import Union

import pandas as pd

from .enums import ErrorBehaviour
from .enums import SecurityIdType
from .utils import log
from .utils.blp_name import ERROR_INFO
from .utils.blp_name import FIELD_DATA
from .utils.blp_name import FIELD_EXCEPTIONS
from .utils.blp_name import FIELD_ID
from .utils.blp_name import MESSAGE
from .utils.blp_name import SECURITY
from .utils.blp_name import SECURITY_DATA
from .utils.blp_name import SECURITY_ERROR
from .utils.exc import BloombergException

# pylint: disable=ungrouped-imports
try:
    import blpapi
except ImportError:
    from async_blp.utils import env_test as blpapi

BloombergValue = Union[str, int, float, dt.date, dt.datetime,
                       Dict[str, Union[str, int, float, dt.date, dt.datetime]]]

LOGGER = log.get_logger()


class ReferenceDataRequest:
    """
    Convenience wrapper around Bloomberg's ReferenceDataRequest
    """
    service_name = "//blp/refdata"
    request_name = "ReferenceDataRequest"

    # pylint: disable=too-many-arguments
    def __init__(self,
                 securities: List[str],
                 fields: List[str],
                 security_id_type: Optional[SecurityIdType] = None,
                 overrides: Optional[Dict] = None,
                 error_behavior: ErrorBehaviour = ErrorBehaviour.RETURN,
                 loop: asyncio.AbstractEventLoop = None,
                 ):

        self._securities = securities
        self._fields = fields
        self._security_id_type = security_id_type
        self._overrides = overrides or {}
        self._error_behaviour = error_behavior

        try:
            self._loop = loop or asyncio.get_running_loop()
            self._msg_queue = asyncio.Queue(loop=self._loop)
        except RuntimeError:
            self._loop = None
            self._msg_queue = None

    def set_running_loop_as_default(self):
        """
        Set currently active loop as default for this request and create
        new message queue
        """
        self._loop = asyncio.get_running_loop()

        if self._msg_queue is not None and not self._msg_queue.empty():
            raise RuntimeError('Current message queue is not empty')

        self._msg_queue = asyncio.Queue()
        LOGGER.debug('%s: loop has been changed', self.__class__.__name__)

    def send_queue_message(self, msg):
        """
        Thread-safe method that put the given msg into async queue
        """
        if self._loop is None or self._msg_queue is None:
            raise RuntimeError('Please create request inside async loop or set '
                               'loop explicitly if you want to use async')

        self._loop.call_soon_threadsafe(self._msg_queue.put_nowait, msg)
        LOGGER.debug('%s: message sent', self.__class__.__name__)

    async def process(self
                      ) -> Tuple[pd.DataFrame, Dict[str,
                                                    Union[str,
                                                          Dict[str, str]]]]:
        """
        Asynchronously process events from `msg_queue` until the event with
        event type RESPONSE is received. This method doesn't check if received
        events belongs to this request and will return everything that
        can be parsed.

        Return format is pd.DataFrame with columns as fields and indexes
        as security_ids.
        """
        dataframe = self._get_empty_df()
        errors = {}

        while True:
            LOGGER.debug('%s: waiting for messages', self.__class__.__name__)
            msg = await self._msg_queue.get()

            if msg is None:
                LOGGER.debug('%s: last message received, processing is '
                             'finished',
                             self.__class__.__name__)
                break

            LOGGER.debug('%s: message received', self.__class__.__name__)

            msg_data = list(msg.getElement(SECURITY_DATA).values())

            for security_data in msg_data:
                msg_frame = self._parse_security_data(security_data)
                index = msg_frame.index
                columns = msg_frame.columns

                dataframe.loc[index, columns] = msg_frame

            for security_data in msg_data:
                security_errors = self._parse_errors(security_data)
                if security_errors:
                    errors.update(security_errors)

        return dataframe, errors

    def _get_security_id_from_security_data(self,
                                            security_data: blpapi.Element):
        """
        Retrieve security id from security data and remove type prefix if needed
        """
        security_id = security_data.getElementAsString(SECURITY)

        if self._security_id_type is not None:
            security_id = self._security_id_type.remove_type(security_id)

        return security_id

    def _parse_security_data(self,
                             security_data,
                             ) -> pd.DataFrame:
        """
        Parse single security data element.

        Return pd.DataFrame with one row and multiple columns corresponding
        to the received fields.
        """
        security_id = self._get_security_id_from_security_data(security_data)

        field_data: blpapi.Element = security_data.getElement(FIELD_DATA)

        security_df = pd.DataFrame()

        for field in field_data.elements():
            field_name, field_value = self._parse_field_data(field)

            if field_name not in security_df and isinstance(field_value, list):
                security_df[field_name] = pd.Series().astype(object)

            security_df.at[security_id, field_name] = field_value

        return security_df

    def _parse_errors(self,
                      security_data,
                      ) -> Optional[Dict[str,
                                         Union[str,
                                               Dict[str, str]]]]:
        """
        Check if the given security data has any errors and process them
        according to `self._error_behaviour`

        Return None if exceptions are ignored, or dict containing security
        and field error messages
        """
        if self._error_behaviour == ErrorBehaviour.IGNORE:
            return None

        security_id = self._get_security_id_from_security_data(security_data)
        security_errors = {}

        if security_data.hasElement(SECURITY_ERROR):
            security_errors[security_id] = 'Invalid security'

        if security_data.hasElement(FIELD_EXCEPTIONS):
            field_exceptions = security_data.getElement(FIELD_EXCEPTIONS)
            field_errors = self._parse_field_exceptions(field_exceptions)

            if field_errors:
                security_errors[security_id] = field_errors

        if self._error_behaviour == ErrorBehaviour.RAISE and security_errors:
            raise BloombergException(security_errors)

        return security_errors

    @staticmethod
    def _parse_field_exceptions(field_exceptions) -> Dict[str, str]:
        """
        Parse field exceptions for one security.

        Return dict {field name : error message}
        """
        errors = {}

        for error in field_exceptions.values():
            field = error.getElementAsString(FIELD_ID)
            error_info = error.getElement(ERROR_INFO)
            message = error_info.getElementAsString(MESSAGE)

            errors[field] = message

        return errors

    def _parse_field_data(self,
                          field: blpapi.Element,
                          ) -> Tuple[str,
                                     Union[BloombergValue,
                                           List[BloombergValue],
                                           List[Dict[str, BloombergValue]]]]:
        """
        Parse single field data element.

        If field data contains bulk data, return list of dicts or list of
        values. Otherwise, return single value
        """

        if field.isArray():
            return self._parse_array_field(field)

        field_name = str(field.name())
        field_value = field.getValue()
        return field_name, field_value

    @staticmethod
    def _parse_array_field(field: blpapi.Element):
        """
        Parse single field that contains bulk data.

        Return field name and field values, either as list of dicts or
        list of values.
        """
        field_name = str(field.name())

        values = [
            {
                str(e1.name()): e1.getValue()
                for e1 in elem.elements()
                }
            for elem in field.values()
            ]

        if values and len(values[0]) == 1:
            values = [list(value.values())[0]
                      for value in values]

        return field_name, values

    def create(self, service: blpapi.Service) -> blpapi.Request:
        """
        Create Bloomberg request. Given `service` must be opened beforehand.
        """
        request = service.createRequest(self.request_name)

        for name in self._securities:
            if self._security_id_type is not None:
                self._security_id_type.add_type(name)

            request.getElement("securities").appendValue(name)

        for field in self._fields:
            request.getElement("fields").appendValue(field)

        for key, value in self._overrides.items():
            request.set(key, value)

        return request

    @property
    def weight(self):
        """
        Approximate number of returned values; used to balance load
        between handlers
        """
        return len(self._securities) * len(self._fields)

    def _get_empty_df(self):
        return pd.DataFrame(columns=self._fields,
                            index=self._securities)


class HistoricalDataRequest(ReferenceDataRequest):
    request_name = 'HistoricalDataRequest'

    # pylint: disable=too-many-arguments
    def __init__(self,
                 securities: List[str],
                 fields: List[str],
                 start_date: dt.date,
                 end_date: dt.date,
                 security_id_type: Optional[SecurityIdType] = None,
                 overrides: Optional[Dict] = None,
                 error_behavior: ErrorBehaviour = ErrorBehaviour.RETURN,
                 loop: asyncio.AbstractEventLoop = None,
                 ):
        self._start_date = start_date
        self._end_date = end_date

        overrides = overrides or {}
        overrides['startDate'] = start_date
        overrides['endDate'] = end_date

        super().__init__(securities, fields, security_id_type,
                         overrides, error_behavior, loop)

    @property
    def weight(self):
        num_days = (self._end_date - self._start_date).days
        return super().weight * num_days

    def _parse_security_data(self, security_data) -> pd.DataFrame:
        security_id = self._get_security_id_from_security_data(security_data)

        field_data: blpapi.Element = security_data.getElement(FIELD_DATA)

        empty_index = pd.MultiIndex.from_tuples([], names=['date', 'security'])
        security_df = pd.DataFrame(index=empty_index)

        for fields_sequence in field_data.elements():
            fields_dict = {}

            for field in fields_sequence.values():
                field_name, field_value = self._parse_field_data(field)
                fields_dict[field_name] = field_value

            date = pd.Timestamp(fields_dict['date'])
            for name, value in fields_dict.items():
                if name == 'date':
                    continue

                security_df.at[(date, security_id), name] = value

        return security_df

    def _get_empty_df(self):
        all_dates = pd.date_range(self._start_date, self._end_date)
        index = pd.MultiIndex.from_product([all_dates, self._securities],
                                           names=['date', 'security'])

        return pd.DataFrame(index=index,
                            columns=self._fields)
