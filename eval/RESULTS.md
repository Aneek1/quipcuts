# Clip-picking evaluation

14 openly licensed English videos (eval/videos.json), 3 clips of 30-60 s per model per video.
Judge: claude-sonnet-5-5, blind, transcript text only, each pair judged in both orders; a side wins only if it
wins both orders, otherwise the pair is a tie. Random cuts are the same length as the pick they're
compared with and start on a sentence boundary.

| Comparison | Pairs | Wins | Losses | Ties | Win rate excl. ties (95% CI) | p (sign test) |
|---|---|---|---|---|---|---|
| Mistral Small 3.2 vs random cut | 42 | 34 | 4 | 4 | 89% (76%-96%) | 0.000 |
| qwen3:8b vs Mistral Small 3.2 | 40 (6 same moment) | 10 | 14 | 10 | 42% (24%-61%) | 0.541 |
| qwen3:8b vs random cut | 40 | 25 | 11 | 4 | 69% (53%-82%) | 0.029 |

Picking time per video on one L4 GPU (median): qwen3:8b 13 s, Mistral Small 3.2 30 s
