"""Qyro — turn long YouTube podcast episodes into short vertical clips.

v0.5.0 "Qyro": rebrand, a dark glass UI, 1440p renders and the offline tool
chain (smart reframe, animated caption brands, beat sync, audio swap, logo
remover, free AI engine, clip tools).

v0.6.0: subject tracking (the camera follows the person instead of a fixed
crop), a caption quality gate that keeps only the good parts of an episode,
per-word caption timing, nine caption animations, ten caption fonts,
Devanagari/Hindi support, clip transitions, and the Android install path.

v0.6.1: the tracker follows the *speaker* (voice-correlated motion scoring,
speaker hand-offs, most-present fallback), shorts are longer (25-90 s) and
always end where the speaker stops, and the free AI engine's defaults,
timeouts and retries were fixed so "AI unavailable during render" only shows
up when the engine genuinely cannot answer.

v0.6.2: new Claude+Grok-inspired logo, captions on/off toggle, flicker-free
Generate buttons (no animation thrash during fastPoll), and the first cut of a
streaming-style intro.

v0.6.3 "spectrum ident": the intro is rebuilt as a glowing Q on black that
bursts outward into a full spectrum of vertical light beams. The beams are
canvas-painted and driven by a live WebAudio analyser, and the ta-dum is
synthesised at runtime (ta, dum, sub, riser, a per-beam arpeggio, a resolving
chord) through a generated reverb. Audio unlock is now gesture-safe: a tap that
exists only to satisfy autoplay starts the score *and* holds the frame so the
hit lands on the burst, instead of cancelling the cue the way v6.2 did.

v0.6.4 "ion": a new logo — a chrome crescent Q lit by an electric-blue rim
glow, crossed by a blade tail, wrapped in a thin orbit with a four-point spark
— and the whole UI retuned to match it. Violet/cyan gives way to ion blue
#1E5BFF, ice chrome #9FC9FF and deep space black #01030B across every surface,
button, glow and focus ring. Colour only: no behaviour changed.

v0.6.5: the search bar no longer draws its icon over the placeholder (the
shared control rule was winning the padding fight), Google's new "AQ." auth
keys work end to end (validator, 422 on wrong-field pastes, automatic rescue
of a Google key pasted into the OpenAI-compatible box, gemini-3.6-flash as
the free-tier default with the Gemini 3 thinkingLevel payload), and the intro
ident's random glitch is gone — envelopes can no longer be scheduled behind
the audio clock, the reverb impulse got cheaper, and a replay fades the old
context out instead of closing it mid-sample.

v0.6.6: two user reports, both about things that looked like a broken app.
The intro ident can no longer strand a full-screen spectrum over Qyro: its
timeline follows the wall clock instead of accumulating frames, a plain timer
dismisses the stage even when requestAnimationFrame never returns, a hidden
tab is put away at once, a lost canvas cannot freeze it, the CSS only shows
the stage when JS asks for it, and a reload minutes after a run does not play
it again. A synthetic demo render is now labelled — burned into the footage,
badged on the short, explained in the inspector — and a placeholder can never
be handed to a YouTube download as if it were the real video. A model id the
provider has retired (gemini-2.5-flash and friends) is no longer called:
Qyro calls the current free-tier default, follows up with the user's own id
when that is what failed, says so in plain words instead of a JSON dump, and
the Settings panel prefill shows the model that is actually live.
"""

__version__ = "0.6.6"
APP_NAME = "Qyro"
