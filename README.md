# Production agent: script in, episode out

One command turns a two-voice script into a published episode.

## Limit line
- Does alone: generate, treat, upload, publish the episode named in the sidecar.
- Checks with you: stops and says why if the sidecar is missing, a line has no speaker, the audio is outside 8 to 25 minutes, or any API call fails.
- Never touches: show settings, other episodes, voices, newsletter, social.

## Set up once (about 10 minutes, do it in Claude Code)
1. Install Python 3.11+ and ffmpeg. `pip install requests`
2. Copy `.env.example` to `.env` and paste your three keys. Transistor show ID is in the show's URL in the dashboard.
3. Run the first episode as a draft: `python produce_episode.py Ep01_Studio_Script.txt`
4. Listen to `out/Ep01_Studio_Script.mp3`. If it is good, set `"publish": true` in the sidecar and run again, or publish the draft in Transistor by hand this one time.

## Every week after that
1. Save the script as `EpNN_Studio_Script.txt` and its sidecar as `EpNN_Studio_Script.json`.
2. `python produce_episode.py EpNN_Studio_Script.txt`
3. Read `out/log.txt`. That is the record.

## Cost
About 9,000 ElevenLabs characters per episode. Transistor Starter plan.
