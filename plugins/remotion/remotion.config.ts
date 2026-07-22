import {Config} from "@remotion/cli/config";

// h264 mp4 — make_video._conform re-encodes it through the overlay graph
// (normalising to yuv420p), so these are just sensible render defaults.
Config.setVideoImageFormat("jpeg");
Config.setCodec("h264");
Config.setOverwriteOutput(true);
