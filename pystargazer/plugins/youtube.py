import asyncio
import datetime
import logging
from dataclasses import dataclass, asdict
from enum import Enum
from functools import partial
from itertools import cycle, tee
from typing import Dict, Iterator, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

import dateutil.parser
import feedparser
from apscheduler.schedulers.base import JobLookupError
from dateutil import tz
from httpx import AsyncClient, NetworkError, TimeoutException
from starlette.endpoints import HTTPEndpoint
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.status import HTTP_404_NOT_FOUND

from pystargazer.app import app
from pystargazer.models import Event
from pystargazer.models import KVPair
from pystargazer.utils import get_option as _get_option


class ResourceType(Enum):
    VIDEO = "video"
    BROADCAST = "broadcast"


class YoutubeEventType(Enum):
    PUBLISH = "publish"
    REMINDER = "reminder"
    SCHEDULE = "schedule"
    LIVE = "live"


@dataclass
class Video:
    video_id: str
    title: str = ""
    link: str = ""
    type: Optional[ResourceType] = None
    description: str = ""
    thumbnail: str = ""
    scheduled_start_time: Optional[datetime.datetime] = None
    actual_start_time: Optional[datetime.datetime] = None

    def dump(self):
        state_dict = asdict(self)
        state_dict["type"] = self.type.name
        state_dict["scheduled_start_time"] = datetime.datetime.timestamp(dt) \
            if (dt := self.scheduled_start_time) else None
        state_dict["actual_start_time"] = datetime.datetime.timestamp(dt) if (dt := self.actual_start_time) else None
        return state_dict

    @classmethod
    def load(cls, state_dict):
        _state_dict = state_dict.copy()
        _state_dict["type"] = ResourceType[state_dict["type"]]
        _state_dict["scheduled_start_time"] = datetime.datetime.fromtimestamp(ts) \
            if (ts := state_dict["scheduled_start_time"]) else None
        _state_dict["actual_start_time"] = datetime.datetime.fromtimestamp(ts) \
            if (ts := state_dict["actual_start_time"]) else None
        return cls(**_state_dict)

    def merge(self, obj):
        if not isinstance(obj, Video) or self.video_id != obj.video_id:
            raise ValueError("Object can't be merged.")
        self.__dict__.update(obj.__dict__)

    async def fetch(self) -> bool:
        while True:
            try:
                r = await http.get("https://www.googleapis.com/youtube/v3/videos", params={
                    "part": "liveStreamingDetails,snippet",
                    "fields": "items(liveStreamingDetails,snippet)",
                    "key": next(token_g),
                    "id": self.video_id
                })
                break
            except (NetworkError, TimeoutException):
                pass

        if not (data := r.json()):
            return False

        try:
            item = data['items'][0]
        except IndexError:
            logging.error(f"Youtube data api malformed response: {data}")
            return False

        if snippet := item.get("snippet"):
            self.description = f'{snippet.get("description")} ...'
            self.thumbnail = thumbnails.get("standard", {"url": None}).get("url") \
                if (thumbnails := snippet.get("thumbnails")) else None

        if streaming := item.get("liveStreamingDetails"):
            self.type = ResourceType.BROADCAST
            if scheduled_start_time := streaming.get("scheduledStartTime"):
                self.scheduled_start_time = dateutil.parser.parse(scheduled_start_time).astimezone(tz.tzlocal())
            if actual_start_time := streaming.get("actualStartTime"):
                self.actual_start_time = dateutil.parser.parse(actual_start_time).astimezone(tz.tzlocal())
        else:
            self.type = ResourceType.VIDEO

        return True


@dataclass
class YoutubeEvent:
    __slots__ = ["type", "event", "channel", "video"]
    type: ResourceType
    event: YoutubeEventType
    channel: str
    video: Video

    def __post_init__(self):
        if self.type == ResourceType.BROADCAST and not self.video.scheduled_start_time:
            raise ValueError("Missing field(s): scheduled_start_time in video.")


token_g: Iterator[str] = cycle(app.credentials.get("youtube"))
callback_url: str = app.credentials.get("base_url")
channel_list: Dict[str, List[Video]] = {}
read_list: List[Video] = []
scheduler = app.scheduler
http = AsyncClient()

get_option = _get_option(app, "youtube")


@app.on_startup
async def startup():
    global channel_list
    # noinspection PyTypeChecker
    async for vtuber in app.vtubers.has_field("youtube"):
        channel_list[vtuber.value["youtube"]] = []

    await load_state()


@app.on_shutdown
async def shutdown():
    await dump_state()


@app.scheduled("interval", minutes=1)
async def state_snapshot():
    await dump_state()


# use one-shot schedule instead of on_startup to ensure callback can handle validation in time
@app.scheduled(None, misfire_grace_time=5)
async def init_subscribe():
    await asyncio.sleep(5)

    channel_ids: List[str] = []
    # noinspection PyTypeChecker
    async for vtuber in app.vtubers.has_field("youtube"):
        channel_ids.append(vtuber.value["youtube"])

    logging.info(f"Subscribing: {channel_ids}")
    await asyncio.gather(*(subscribe(channel_id) for channel_id in channel_ids))
    logging.info("Subscribe finished")


# noinspection PyUnusedLocal
@app.route("/help/youtube", methods=["GET"])
async def youtube_help(request: Request):
    return PlainTextResponse(
        "Field: youtube\n"
        "Configs[/configs/youtube]:\n"
        "  video_disabled live_disabled reminder_disabled schedule_disabled"
    )


async def get_vtuber(channel_id: str) -> KVPair:
    # noinspection PyTypeChecker
    async for vtuber in app.vtubers.has_field("youtube"):
        if vtuber.value["youtube"] == channel_id:
            return vtuber


async def send_youtube_event(ytb_event: YoutubeEvent):
    # noinspection PyTypeChecker
    vtuber = await get_vtuber(ytb_event.channel)
    video = ytb_event.video

    event: Optional[Event] = None
    if ytb_event.type == ResourceType.VIDEO and not await get_option("video_disabled"):
        event = Event("youtube_video", vtuber.key, {
            "title": video.title,
            "description": video.description,
            "images": [video.thumbnail],
            "link": video.link
        })
    elif ytb_event.type == ResourceType.BROADCAST:
        scheduled_start_time_print = video.scheduled_start_time.strftime("%Y-%m-%d %I:%M%p (CST)")
        if ytb_event.event == YoutubeEventType.LIVE and not await get_option("live_disabled"):
            actual_start_time_print = video.actual_start_time.strftime("%Y-%m-%d %I:%M%p (CST)")
            event = Event("youtube_broadcast_live", vtuber.key, {
                "title": video.title,
                "description": video.description,
                "link": video.link,
                "images": [video.thumbnail],
                "scheduled_start_time": scheduled_start_time_print,
                "actual_start_time": actual_start_time_print
            })
        elif ytb_event.event == YoutubeEventType.REMINDER and not await get_option("reminder_disabled"):
            event = Event("youtube_broadcast_reminder", vtuber.key, {
                "title": video.title,
                "description": video.description,
                "link": video.link,
                "images": [video.thumbnail],
                "scheduled_start_time": scheduled_start_time_print,
            })
        elif ytb_event.event == YoutubeEventType.SCHEDULE and not await get_option("schedule_disabled"):
            event = Event("youtube_broadcast_schedule", vtuber.key, {
                "title": video.title,
                "description": video.description,
                "link": video.link,
                "images": [video.thumbnail],
                "scheduled_start_time": scheduled_start_time_print,
            })
    if event:
        await app.send_event(event)


async def _subscribe(channel_id: str, reverse: bool = False):
    while True:
        try:
            await http.post("https://pubsubhubbub.appspot.com/subscribe", data={
                "hub.callback": urljoin(callback_url, f"youtube_callback"),
                "hub.topic": f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={channel_id}",
                "hub.verify": "async",
                "hub.mode": "subscribe" if not reverse else "unsubscribe",
                "hub.lease_seconds": 86400
            })
            break
        except (NetworkError, TimeoutException):
            pass


async def subscribe(channel_id: str):
    if channel_id not in channel_list:
        channel_list[channel_id] = []
    await _subscribe(channel_id)


async def unsubscribe(channel_id: str, pop: bool = True):
    if channel_list.get(channel_id) is None:
        raise ValueError("Not found.")

    for video in channel_list[channel_id]:
        try:
            scheduler.remove_job(f'reminder_{channel_id}_{video.video_id}')
        except JobLookupError:
            pass

    if pop:
        channel_list.pop(channel_id)

    await _subscribe(channel_id, True)


@app.route("/youtube_callback", methods=["GET", "POST"])
class WebsubEndpoint(HTTPEndpoint):
    # noinspection PyMethodMayBeStatic
    async def get(self, request: Request):
        topic = request.query_params["hub.topic"]
        challenge = request.query_params["hub.challenge"]
        mode = request.query_params["hub.mode"]

        channel_id = parse_qs(urlparse(topic).query).get("channel_id")[0]

        accept = (mode == "subscribe" and channel_id in channel_list) or (
                mode == "unsubscribe" and channel_id not in channel_list)

        if not accept:
            logging.info(f"Rejecting {mode}: {channel_id}")
            return Response(None, status_code=HTTP_404_NOT_FOUND)

        logging.info(f"Accepting {mode}: {channel_id}")
        return PlainTextResponse(challenge)

    # noinspection PyMethodMayBeStatic
    async def post(self, request: Request):
        def parse_feed(data: str) -> Tuple[str, str, str, str]:
            feed = feedparser.parse(data)
            entry = feed.entries[0]
            return entry.yt_videoid, entry.link, entry.title, entry.yt_channelid

        body = (await request.body()).decode("utf-8")
        logging.debug(body)
        if "deleted-entry" in body:
            return Response()

        video_id, video_link, video_title, channel_id = parse_feed(body)
        video = Video(video_id)

        logging.info(f"Adding video {video_id}")

        if not await video.fetch():
            logging.warning("Query failure. Ignoring.")
            return Response()

        if video.type == ResourceType.VIDEO:
            # check whether the video is already in the read_list
            if not next(_video for _video in read_list if video.video_id == _video.video_id):
                event = YoutubeEvent(type=video.type, event=YoutubeEventType.PUBLISH, channel=channel_id, video=video)
                await send_youtube_event(event)
                read_list.append(video)
        elif video.type == ResourceType.BROADCAST and not video.actual_start_time:
            if not video.scheduled_start_time:
                logging.warning("Malformed video object: missing scheduled start time.")
                return Response()

            try:
                existing_entry = \
                    next(_video for _video in channel_list[channel_id] if video.video_id == _video.video_id)
                logging.debug("Duplicate video id detected. Checking...")
            except StopIteration:
                existing_entry = None

            dup = existing_entry and all([
                existing_entry.title == video.title,
                existing_entry.scheduled_start_time == video.scheduled_start_time
            ])

            if dup:
                logging.info("Duplicate entry. Ignoring.")
                return Response()

            if existing_entry:
                logging.info("Merging new state into existing entry.")
                existing_entry.merge(video)
                video = existing_entry
            else:
                channel_list[channel_id].append(video)  # for actual start event

            event_schedule = YoutubeEvent(type=video.type, event=YoutubeEventType.SCHEDULE,
                                          channel=channel_id, video=video)
            event_reminder = YoutubeEvent(type=video.type, event=YoutubeEventType.REMINDER,
                                          channel=channel_id, video=video)

            # set a reminder
            job_id = f"reminder_{channel_id}_{video.video_id}"
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id=job_id)
            scheduler.add_job(partial(send_youtube_event, event_reminder), trigger="cron", id=job_id,
                              year=video.scheduled_start_time.year, month=video.scheduled_start_time.month,
                              day=video.scheduled_start_time.day, hour=video.scheduled_start_time.hour,
                              minute=video.scheduled_start_time.minute,
                              second=video.scheduled_start_time.second)

            # for scheduled
            await send_youtube_event(event_schedule)

        return Response()


# noinspection PyUnusedLocal
@app.on_update("vtubers")
async def on_update(obj: KVPair, added: dict, removed: dict, updated: dict):
    if "youtube" in added:
        await subscribe(added["youtube"])
    elif "youtube" in removed:
        await unsubscribe(removed["youtube"])
    elif "youtube" in updated:
        old_id, new_id = updated["youtube"]
        await unsubscribe(old_id)
        await subscribe(new_id)


@app.on_delete("vtubers")
async def on_delete(obj: KVPair):
    if yid := obj.value.get("youtube"):
        await unsubscribe(yid)


@app.scheduled("interval", minutes=1, id="ytb_tick")
async def tick():
    def batch_remove(iterable: Iterator[Tuple[str, Video]]):
        for ch_id, video in iterable:
            channel_list[ch_id].remove(video)

    def split(seq, condition):
        l1, l2 = tee((condition(item), item) for item in seq)
        return (i for p, i in l1 if p), (i for p, i in l2 if not p)

    async def check_send(ch_id, video) -> bool:
        """ send message and return is_delete """
        now = datetime.datetime.now().replace(tzinfo=tz.tzlocal())
        if not video.scheduled_start_time:
            logging.warning(f"Video {video.video_id} doesn't have scheduled start time.")
            return True
        if (now - video.scheduled_start_time).total_seconds() > -600 and video.actual_start_time:
            if (now - video.actual_start_time).total_seconds() < 10800: # broadcast has started
                event = YoutubeEvent(type=ResourceType.BROADCAST, event=YoutubeEventType.LIVE,
                                     channel=ch_id, video=video)
                await send_youtube_event(event)
            return True
        return False

    # batch update objects
    video_list: List[Tuple[str, Video]] = [(channel, video)
                                           for channel, videos in channel_list.items()
                                           for video in videos]
    # noinspection PyTypeChecker
    fetch_map: List[Tuple[Tuple[str, Video], bool]] = zip(
        video_list,
        (await asyncio.gather(*(video.fetch() for _, video in video_list))))
    # remove failed objects
    success_map: List[Tuple[str, Video]]
    error_map: List[Tuple[str, Video]]
    success_map, error_map = [[x[0] for x in iterable] for iterable in split(fetch_map, lambda x: x[1])]
    # noinspection PyTypeChecker
    send_map: Iterator[Tuple[Tuple[str, Video], bool]] = zip(
        success_map,
        (await asyncio.gather(*(check_send(*video_tuple) for video_tuple in success_map)))
    )
    remove_map: Iterator[Tuple[str, Video]] = map(lambda x: x[0], filter(lambda x: x[1], send_map))

    batch_remove(error_map)
    batch_remove(remove_map)


@app.scheduled("interval", hours=8, id="ytb_renewal")
async def renewal():
    for channel_id in channel_list:
        await _subscribe(channel_id)


# @app.on_shutdown
async def cleanup():
    for channel_id in channel_list:
        await unsubscribe(channel_id, pop=False)
    channel_list.clear()
    scheduler.remove_job("ytb_tick")
    scheduler.remove_job("ytb_renewal")


async def load_state():
    global channel_list
    global read_list
    try:
        channel_state = await app.plugin_state.get("youtube_live_state")
    except KeyError:
        logging.warning("Missing live state dict. Ignoring.")
        channel_state = KVPair("youtube_live_state", {})
    try:
        read_state = await app.plugin_state.get("youtube_video_state")
    except KeyError:
        logging.warning("Missing video state dict. Ignoring.")
        read_state = KVPair("youtube_video_state", {"videos": []})

    for channel, videos in channel_state.value.items():
        for _video in videos:
            video = Video.load(_video)
            await video.fetch()
            if not video.actual_start_time:
                logging.debug(f"Load saved broadcast: {video}")
                event_reminder = YoutubeEvent(type=video.type, event=YoutubeEventType.REMINDER,
                                              channel=channel, video=video)

                # set a reminder
                job_id = f"reminder_{channel}_{video.video_id}"
                scheduler.add_job(partial(send_youtube_event, event_reminder), trigger="cron", id=job_id,
                                  year=video.scheduled_start_time.year, month=video.scheduled_start_time.month,
                                  day=video.scheduled_start_time.day, hour=video.scheduled_start_time.hour,
                                  minute=video.scheduled_start_time.minute,
                                  second=video.scheduled_start_time.second)
                channel_list[channel].append(video)

    read_list = [Video.load(video) for video in read_state.value["videos"]]


async def dump_state():
    channel_state = {channel: [video.dump() for video in videos] for channel, videos in channel_list.items()}
    read_state = {"videos": [video.dump() for video in read_list]}

    await app.plugin_state.put(KVPair("youtube_live_state", channel_state))
    await app.plugin_state.put(KVPair("youtube_video_state", read_state))
