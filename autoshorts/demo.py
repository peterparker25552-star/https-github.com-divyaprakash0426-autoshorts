"""Demo mode: runs the *real* highlight + render pipeline over synthetic
media, so AutoShorts can be evaluated end-to-end on machines that cannot
reach YouTube (e.g. restricted sandboxes).

Episode titles are real entries from the seed playlist
("Figuring Out With Raj Shamani"); media and transcripts are synthetic.
"""
from __future__ import annotations

from pathlib import Path

from . import config
from .ffmpeg import synth_demo_video
from .transcripts import Segment

DEMO_DURATION = 96  # seconds of synthetic source per episode

DEMO_EPISODES = [
    {
        "id": "demo-chhetri-223",
        "real_id": "gHQo3UafM54",
        "title": "Sunil Chhetri On Indian Football, Retirement, Love Life, "
        "Family & Virat Kohli | FO 223 (Demo)",
        "url": "https://www.youtube.com/watch?v=gHQo3UafM54",
        "duration": DEMO_DURATION,
        "hue": 0,
    },
    {
        "id": "demo-dig-221",
        "real_id": "TYqyvHds_58",
        "title": "Human Trafficking, Child Crime, CBI Raid, S*xual "
        "Exploitation - Ex DIG In CBI | FO 221 (Demo)",
        "url": "https://www.youtube.com/watch?v=TYqyvHds_58",
        "duration": DEMO_DURATION,
        "hue": 60,
    },
    {
        "id": "demo-diljit-215",
        "real_id": "k7EJ_9gaWPA",
        "title": "Diljit Dosanjh On Music, Love Life, Childhood, Bollywood, "
        "Money, SRK & India | FO 215 (Demo)",
        "url": "https://www.youtube.com/watch?v=k7EJ_9gaWPA",
        "duration": DEMO_DURATION,
        "hue": 120,
    },
]

# Segments: (start, end, text). Hinglish-flavoured podcast lines with hooks,
# numbers, questions and emotional beats so the highlight engine has real
# signal to score. Timestamps span the 96s synthetic media.
DEMO_TRANSCRIPTS: dict[str, list[tuple[float, float, str]]] = {
    "demo-chhetri-223": [
        (0.0, 4.5, "Welcome back to Figuring Out, today we have Sunil Chhetri with us."),
        (4.5, 9.0, "Before we start, this episode is sponsored, check the description for links."),
        (9.0, 14.0, "So Sunil, let me start with the question everyone asks you."),
        (14.0, 20.0, "Why did you retire? What was going through your head that night?"),
        (20.0, 26.5, "The truth is, I had cried in the dressing room more than once that season."),
        (26.5, 33.0, "Nobody tells you this about sport — the body gives up before the mind does."),
        (33.0, 39.0, "I remember when I scored my 94th goal and I felt nothing. Nothing at all."),
        (39.0, 45.5, "People think 150 international matches is a happy number. For me it was 150 chances to fail."),
        (45.5, 51.0, "And I failed. A lot. The biggest mistake athletes make is hiding the fear."),
        (51.0, 57.5, "Here's the thing — Indian football loses 40 percent of its talent before age 14."),
        (57.5, 63.0, "40 percent! Parents pull kids out because there is no money in the game."),
        (63.0, 69.0, "One day a father came to me and said, beta, football se roti nahi milegi."),
        (69.0, 75.0, "I told him, give me two years. Today his son plays for a top club in Bengaluru."),
        (75.0, 81.0, "Virat told me something once that changed my life completely."),
        (81.0, 87.0, "He said, the day you stop being scared of losing, you have already lost."),
        (87.0, 92.0, "That hit me hard. Honestly, I still get scared before every single match."),
        (92.0, 96.0, "That fear is my fuel now."),
    ],
    "demo-dig-221": [
        (0.0, 5.0, "Welcome to the show. Today's guest spent 20 years in the CBI."),
        (5.0, 9.5, "Subscribe and share the video if you like such conversations."),
        (9.5, 15.0, "Sir, the first raid you ever led — what happened that night?"),
        (15.0, 21.5, "What most people don't know is that a raid can go wrong in 90 seconds."),
        (21.5, 28.0, "In one case, we recovered 9 crore in cash from inside a false ceiling."),
        (28.0, 34.5, "The shocking part? The family was living in a small rented flat."),
        (34.5, 41.0, "Child trafficking numbers in India will shock you. Over 100 cases every day."),
        (41.0, 47.5, "Every single day. And those are only the cases that get reported."),
        (47.5, 54.0, "The biggest mistake we made as investigators was trusting local informers blindly."),
        (54.0, 60.5, "I remember when a raid collapsed because someone tipped off the target."),
        (60.5, 66.5, "Sach bataun? That night I realised the system is broken from inside."),
        (66.5, 72.5, "But here's the thing — one honest officer can still break an entire syndicate."),
        (72.5, 78.5, "We rescued 34 children in one operation. 34 children in a single night."),
        (78.5, 84.5, "When those kids held my hand, honestly, I cried in the police jeep."),
        (84.5, 90.0, "People ask me, why did you retire early? The truth is, the fear never left."),
        (90.0, 96.0, "But fear and duty, when they walk together, that is service."),
    ],
    "demo-diljit-215": [
        (0.0, 5.0, "Namaskar, welcome back to Figuring Out with Diljit Dosanjh."),
        (5.0, 9.0, "Diljit paaji, first question — music, money or love? Rank them."),
        (9.0, 15.0, "Honestly? Music is my first everything. Money was never the plan."),
        (15.0, 21.5, "I remember when I bought my first tabla with 300 rupees borrowed money."),
        (21.5, 27.5, "300 rupees! And today people offer me 5 crore for one show. I say no."),
        (27.5, 33.5, "Why do you say no? That is the question everyone asks me."),
        (33.5, 39.5, "Because the day money becomes bigger than the music, the music dies."),
        (39.5, 45.5, "One day I was recording in a studio in Ludhiana, no AC, 45 degrees."),
        (45.5, 51.5, "That song crossed 100 million views. Nobody believed it would work."),
        (51.5, 57.5, "The truth is, Bollywood rejected me three times before everything changed."),
        (57.5, 63.5, "Three times! I cried in my gaddi after the third rejection, I was broke."),
        (63.5, 69.5, "My mother sold her gold kadaan so I could make my first album."),
        (69.5, 75.5, "When I perform at Coachella now, I think of that day every single time."),
        (75.5, 81.5, "SRK once told me, beta, teri mehnat teri pehchaan hai. That hit deep."),
        (81.5, 87.5, "Punjab, India, the world — representation matters. That is my only goal."),
        (87.5, 96.0, "So no, I will never leave my roots. Never."),
    ],
}


def demo_segments(ep_id: str) -> list[Segment]:
    return [
        Segment(s, e, t)
        for s, e, t in DEMO_TRANSCRIPTS[ep_id]
    ]


def prepare_demo_media(ep: dict) -> Path:
    dest = config.MEDIA_DIR / f"{ep['id']}.mp4"
    return synth_demo_video(dest, duration=DEMO_DURATION, hue=ep.get("hue", 0))


def is_demo(episode: dict) -> bool:
    return str(episode.get("source", "")) == "demo" or episode["id"].startswith("demo-")
