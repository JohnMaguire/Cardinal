import datetime
import logging
import re
from collections import OrderedDict, defaultdict

import pytz
import requests
from twisted.internet import defer, error, reactor
from twisted.internet.task import deferLater
from twisted.internet.threads import deferToThread

from cardinal.bot import user_info
from cardinal.decorators import regex

# Alpha Vantage API key
AV_API_URL = "https://www.alphavantage.co/query"

# This is actually max tries, not max retries (for API requests)
MAX_RETRIES = 3
RETRY_WAIT = 15

# Looks for !check followed by a symbol
# Supports relayed messages.
CHECK_REGEX = r'^(?:<(.+?)>\s+)?!check ([A-Za-z]+)$'

# Looks for !predict followed by a symbol, followed by a decimal or whole
# number, optionally followed by a percentage sign.
# Supports relayed messages.
PREDICT_REGEX = r'^(?:<(.+?)>\s+)?!predict ([A-za-z]+) ([-+])?(\d+(?:\.\d+)?)%$'


class ThrottledException(Exception):
    """An exception we raise when we believe we are being API throttled."""
    pass


def est_now():
    tz = pytz.timezone('America/New_York')
    now = datetime.datetime.now(tz)

    return now


def market_is_open():
    """Not aware of holidays or anything like that..."""
    now = est_now()

    # Determine if the market is currently open
    is_market_closed = (now.weekday() >= 5) or \
            (now.hour < 9 or now.hour >= 17) or \
            (now.hour == 9 and now.minute < 30) or \
            (now.hour == 16 and now.minute > 0)

    return not is_market_closed


def sleep(secs):
    return deferLater(reactor, secs, lambda: None)


def get_delta(new_value, old_value):
    return float(new_value) / float(old_value) * 100 - 100


def colorize(percentage):
    if percentage > 0:
        return '\x0309{:.2f}%\x03'.format(percentage)
    else:
        return '\x0304{:.2f}%\x03'.format(percentage)


class TickerPlugin(object):
    def __init__(self, cardinal, config):
        self.logger = logging.getLogger(__name__)
        self.cardinal = cardinal

        self.config = config or {}
        self.config.setdefault('api_key', None)
        self.config.setdefault('channels', [])
        self.config.setdefault('stocks', [])
        self.config.setdefault('relay_bots', [])

        if not self.config["channels"]:
            self.logger.warning("No channels for ticker defined in config --"
                                "ticker will be disabled")
        if not self.config["stocks"]:
            self.logger.warning("No stocks for ticker defined in config -- "
                                "ticker will be disabled")

        if not self.config["api_key"]:
            raise KeyError("Missing required api_key in ticker config")
        if len(self.config["stocks"]) > 5:
            raise ValueError("No more than 5 stocks may be present in ticker "
                             "config")

        self.relay_bots = []
        for relay_bot in self.config['relay_bots']:
            user = user_info(
                relay_bot['nick'],
                relay_bot['user'],
                relay_bot['vhost'])
            self.relay_bots.append(user)

        self.predictions = defaultdict(dict)

        self.call_id = None
        self.wait()

    def is_relay_bot(self, user):
        """Compares a user against the registered relay bots."""
        for bot in self.relay_bots:
            if (bot.nick is None or bot.nick == user.nick) and \
                    (bot.user is None or bot.user == user.user) and \
                    (bot.vhost is None or bot.vhost == user.vhost):
                return True

        return False

    def wait(self):
        """Tell the reactor to call tick() at the next 15 minute interval"""
        now = est_now()
        minutes_to_sleep = 15 - now.minute % 15
        seconds_to_sleep = minutes_to_sleep * 60
        seconds_to_sleep = seconds_to_sleep - now.second

        self.call_id = reactor.callLater(minutes_to_sleep * 60, self.tick)

    def close(self, cardinal):
        if self.call_id:
            try:
                self.call_id.cancel()
            except error.AlreadyCancelled as e:
                self.logger.debug(e)

    @defer.inlineCallbacks
    def tick(self):
        """Send a message with daily stock movements"""
        # If it's after 4pm ET or before 9:30am ET on a weekday, or if it's
        # a weekend (Saturday or Sunday), don't tick, just wait.
        now = est_now()

        # Determine if the market is currently open
        is_market_open = not ((now.weekday() >= 5) or \
                (now.hour < 9 or now.hour >= 17) or \
                (now.hour == 9 and now.minute < 30) or \
                (now.hour == 16 and now.minute > 0))

        # Determine if this is the market opening or market closing
        is_open = now.hour == 9 and now.minute == 30
        is_close = now.hour == 16 and now.minute == 0

        # Determine if we should do predictions after sending ticker
        do_predictions = True if is_open or is_close else False

        # If there are no stocks to send in the ticker, or no channels to send
        # them to, don't tick, just wait.
        should_send_ticker = is_market_open and \
            self.config["channels"] and self.config["stocks"]

        if should_send_ticker:
            yield self.send_ticker()

        # Start the timer for the next tick
        self.wait()

        if do_predictions:
            # Try to avoid hitting rate limiting (5 calls per minute) by giving
            # a minute of buffer after the ticker.
            yield sleep(60)
            yield self.do_predictions()

    @defer.inlineCallbacks
    def send_ticker(self):
        # Hopefully our stocks are in the originally specified order, so let's
        # try to keep results in that order too (it's unclear to me whether
        # this is working properly or not.)
        results = OrderedDict({symbol: None
                               for symbol in self.config["stocks"]})

        # Used a DeferredList so that we can make requests for all the symbols
        # we care about simultaneously
        deferreds = []
        for symbol, name in self.config["stocks"].items():
            deferreds.append(self.get_daily_change(symbol))
        dl = defer.DeferredList(deferreds)

        # Loop the results, ignoring errored requests
        dl_results = yield dl
        for success, result in dl_results:
            if not success:
                self.logger.error(
                    "Error fetching symbol {} for ticker -- skipping: {}"
                    .format(result.getErrorMessage()))
                del results[symbol]
            else:
                symbol, change = result
                results[symbol] = change

        # Format and send the results
        message_parts = []
        for symbol, result in results.items():
            message_parts.append(self.format_symbol(symbol, change))

        message = ' | '.join(message_parts)
        for channel in self.config["channels"]:
            self.cardinal.sendMsg(channel, message)

    def format_symbol(self, symbol, change):
        return "{name} (\x02{symbol}\x02): {change}".format(
                symbol=symbol,
                name=self.config["stocks"][symbol],
                change=colorize(change),
            )

    @defer.inlineCallbacks
    def do_predictions(self):
        # Loop each prediction, grouped by symbols to avoid rate limits
        for symbol in self.predictions:
            try:
                data = yield self.get_daily(symbol)

                # If the market just opened, grab the open
                if market_is_open():
                    actual = data['open']
                # If it just closed, grab the close
                else:
                    actual = data['close']
            except Exception as e:
                self.logger.exception(
                    "Failed to fetch information for symbol {} -- skipping"
                    .format(symbol))
                for channel in self.config["channels"]:
                    self.cardinal.sendMsg(
                        channel, "Error with predictions for symbol {}.".format(
                            symbol))
                continue

            # Loop each nick's prediction, and look for the closest prediction
            # for the current symbol
            closest_prediction = None
            closest_delta = None
            closest_nick = None
            for nick, data in self.predictions[symbol].items():
                datetime, base, prediction = data

                # Check if this is the closest guess for the symbol so far
                delta = abs(actual - prediction)
                if not closest_delta or delta < closest_delta:
                    closest_prediction = prediction
                    closest_delta = delta
                    closest_nick = nick

                self.send_prediction(
                    nick,
                    symbol,
                    actual,
                )

            for channel in self.config["channels"]:
                self.cardinal.sendMsg(
                    channel,
                    "{} had the closest guess for \x02{}\x02 out of {} "
                    "predictions with a prediction of {} ({}).".format(
                        closest_nick,
                        symbol,
                        len(self.predictions[symbol]),
                        closest_prediction,
                        colorize(get_delta(closest_prediction, actual)),
                    ))

            # Try to avoid hitting rate limiting (5 calls per minute) by
            # only checking predictions of 4 symbols per minute
            yield sleep(15)

        # Clear all predictions
        self.predictions = defaultdict(dict)

    def send_prediction(
        self,
        nick,
        symbol,
        actual,
    ):
        market_open_close = 'open' if market_is_open() else 'close'
        dt, base, prediction = self.predictions[symbol][nick]

        for channel in self.config["channels"]:
            self.cardinal.sendMsg(
                channel,
                "Prediction by {} for \x02{}\x02: {} ({}). "
                "Actual value at {}: {} ({}). "
                "Prediction set at {}.".format(
                    nick,
                    symbol,
                    prediction,
                    colorize(get_delta(prediction, base)),
                    market_open_close,
                    actual,
                    colorize(get_delta(actual, base)),
                    dt.strftime('%Y-%m-%d %H:%M:%S %Z')
                ))

    @regex(CHECK_REGEX)
    @defer.inlineCallbacks
    def check(self, cardinal, user, channel, msg):
        """Check a specific stock for current value and daily change"""
        nick = user.nick

        match = re.match(CHECK_REGEX, msg)
        if match.group(1):
            # this group should only be present when a relay bot is relaying a
            # message for another user
            if not self.is_relay_bot(user):
                return

            nick = match.group(1)

        symbol = match.group(2).upper()
        try:
            data = yield self.get_daily(symbol)
        except Exception as exc:
            self.logger.warning("Error trying to look up symbol {}: {}".format(
                symbol, exc))
            cardinal.sendMsg(
                channel, "{}: Is your symbol correct?".format(nick))
            return

        cardinal.sendMsg(
            channel,
            "Symbol: \x02{}\x02 | Current: {} | Daily Change: {}".format(
                symbol,
                data['current'],
                colorize(data['percentage'])))

    @regex(PREDICT_REGEX)
    @defer.inlineCallbacks
    def predict(self, cardinal, user, channel, msg):
        # Parse prediction - this may fail if we matched the relay bot regex
        # but a relay bot didn't send the message
        prediction = yield self.parse_prediction(user, msg)
        if prediction is None:
            return
        nick, symbol, prediction, base = prediction

        # If the user already had a prediction for the symbol, create a message
        # with the old prediction's info
        try:
            dt, old_base, old_prediction = self.predictions[symbol][nick]
        except KeyError:
            old_str = ''
        else:
            old_str = '(replaces old prediction of {:.2f} ({}) set at {})' \
                .format(
                    old_prediction,
                    colorize(get_delta(old_prediction, old_base)),
                    old_datetime.strftime('%x %X %Z'),
                )

        # Save the prediction
        self.save_prediction(symbol, nick, base, prediction)
        cardinal.sendMsg(
            channel,
            "Prediction by {} for \x02{}\x02 at market {}: {:.2f} ({}) {}"
            .format(nick,
                    symbol,
                    'close' if market_is_open() else 'open',
                    prediction,
                    colorize(get_delta(prediction, base)),
                    old_str))

    @defer.inlineCallbacks
    def parse_prediction(self, user, message):
        match = re.match(PREDICT_REGEX, message)

        # Fix nick if relay bot sent the message
        nick = user.nick
        if match.group(1):
            if not self.is_relay_bot(user):
                defer.returnValue(None)

            nick = match.group(1)

        # Convert symbol to uppercase
        symbol = match.group(2).upper()

        data = yield self.get_daily(symbol)
        if market_is_open():
            # get value at open
            base = data['open']
        else:
            # get value at close
            base = data['close']

        prediction = float(match.group(4))
        negative = match.group(3) == '-'

        prediction = prediction * .01 * base
        if negative:
            prediction = base - prediction
        else:
            prediction = base + prediction

        defer.returnValue((
            nick,
            symbol,
            prediction,
            base,
        ))

    def save_prediction(self, symbol, nick, base, prediction):
        self.predictions[symbol][nick] = (est_now(), base, prediction)

    @defer.inlineCallbacks
    def get_daily_change(self, symbol):
        res = yield self.get_daily(symbol)
        defer.returnValue((symbol, res['percentage']))

    @defer.inlineCallbacks
    def get_daily(self, symbol):
        data = yield self.get_time_series_daily(symbol)

        # This may not actually be today if it's the morning before the market
        # opens, or the weekend
        today = est_now()
        count = 0
        while data.get(today.strftime('%Y-%m-%d'), None) is None and count < 5:
            count += 1
            today = today - datetime.timedelta(days=1)
        if data.get(today.strftime('%Y-%m-%d'), None) is None:
            raise Exception("Can't find data as far back as {}".format(today))

        # This may not actually be the day prior to "today" if it's a Monday
        # for example (then last_day would be the preceding Friday)
        last_day = today - datetime.timedelta(days=1)
        count = 0
        while data.get(last_day.strftime('%Y-%m-%d'), None) is None and count < 5:
            count += 1
            last_day = last_day - datetime.timedelta(days=1)
        if data.get(last_day.strftime('%Y-%m-%d'), None) is None:
            raise Exception("Can't find data as far back as {}".format(last_day))

        current_value = data[today.strftime('%Y-%m-%d')]
        last_day_value = data[last_day.strftime('%Y-%m-%d')]

        percentage = get_delta(last_day_value['close'], current_value['close'])
        defer.returnValue({'current': current_value['close'],
                           'close': current_value['close'],
                           'open': current_value['open'],
                           'percentage': percentage,
                           })

    @defer.inlineCallbacks
    def get_time_series_daily(self, symbol, outputsize='compact'):
        data = yield self.make_av_request('TIME_SERIES_DAILY',
                                          {'symbol': symbol,
                                           'outputsize': outputsize,
                                           })
        try:
            data = data['Time Series (Daily)']
        except KeyError:
            raise KeyError("Response missing expected 'Time Series (Daily)' "
                           "key: {}".format(data))

        for date, values in data.items():
            # Strip prefixes like "4. " from "4. close" and convert values from
            # the API to float instead of string
            values = {k[3:]: float(v) for k, v in values.items()}
            data[date] = values

        defer.returnValue(data)


    @defer.inlineCallbacks
    def make_av_request(self, function, params=None):
        if params is None:
            params = {}
        params['function'] = function
        params['apikey'] = self.config["api_key"]
        params['datatype'] = 'json'

        retries_remaining = MAX_RETRIES
        while retries_remaining > 0:
            retries_remaining -= 1
            try:
                r = yield deferToThread(requests.get,
                                        AV_API_URL,
                                        params=params)
                result = r.json()

                # Detect rate limiting
                if 'Note' in result and 'call frequency' in result['Note']:
                    raise ThrottledException(result['Note'])
            except Exception:
                self.logger.exception("Failed to make request to AV API - "
                                      "retries remaining: {}".format(
                                          retries_remaining))

                # Raise the exception if we're out of retries
                if retries_remaining == 0:
                    raise
            # If we succeeded, don't retry
            else:
                break

            # Otherwise, sleep 15 seconds to avoid rate limits before retrying
            yield sleep(RETRY_WAIT)
            continue

        defer.returnValue(result)


def setup(cardinal, config):
    return TickerPlugin(cardinal, config)
