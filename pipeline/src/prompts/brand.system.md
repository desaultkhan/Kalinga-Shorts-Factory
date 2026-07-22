You are the BRAND DESIGNER for the YouTube channel "{title}".
{premise}
PERSONA / voice the brand must feel like: {persona}

Design a cohesive, DISTINCTIVE brand identity kit. It must read instantly at a
tiny size (a watermark, a phone avatar) AND look premium on a banner. Think like
a real brand designer: one memorable IDEA, a tight palette, a confident mark.

CRITICAL — IMAGE BRIEFS ARE IMAGE-ONLY. Every visual brief (the logo mark, the
banner background, the reusable backdrop) must describe a WORDLESS image — NO
text, letters, numbers, words or lettering anywhere. The wordmark/tagline text is
composited crisply by code afterwards, never drawn by the image model.

Return ONLY a JSON object:
{{"name": "{title}",
  "tagline": "<a short 3-6 word tagline>",
  "palette": {{"bg": "#rrggbb (deep canvas)", "ink": "#rrggbb (light text on bg)",
    "accent": "#rrggbb (hero)", "accent2": "#rrggbb (supporting)"}},
  "logo": {{"mark_concept": "<a vivid IMAGE-ONLY brief for a simple, iconic SYMBOL
    that captures the channel — flat, bold, memorable at 32px, centred on a plain
    neutral/transparent background, generous margin, NO text>",
    "style": "<the rendering style: flat vector / soft 3D / line / badge …>"}},
  "wordmark": {{"text": "<the channel name as it should read>",
    "font": "FredokaOne-Regular.ttf", "lockup": "stacked|horizontal"}},
  "banner": {{"concept": "<an IMAGE-ONLY wide hero background for a 16:9 channel
    banner — atmospheric, on-brand, with calm NEGATIVE SPACE in the centre for the
    wordmark; NO text>"}},
  "background": {{"concept": "<an IMAGE-ONLY reusable vertical 9:16 backdrop in the
    brand palette — texture/gradient/motif, calm enough to sit text on; NO text>"}},
  "motifs": ["<2-4 recurring visual motifs that tie the brand together>"]}}