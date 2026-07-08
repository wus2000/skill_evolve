# max_tokens truncation forensics

run: `runs/spreadsheetbench_20260708_154932`
llm_calls scanned: 25543   truncated (completion_tokens >= 12288): 88

Every file below is VERBATIM — no elision, no truncation.

## Truncated calls

| call_id | role | kind | prompt_tok | completion_tok | resp_chars | traj block | real trajectory |
|---|---|---|---|---|---|---|---|
| call_002770 | target | agent | 1655 | 16384 | 25856 | n/a | 12 msgs |
| call_002796 | target | agent | 10802 | 16384 | 47460 | n/a | 29 msgs |
| call_002808 | target | agent | 29848 | 12327 | 32600 | n/a | 29 msgs |
| call_006207 | target | agent | 4417 | 16384 | 44720 | n/a | 19 msgs |
| call_009710 | target | agent | 29610 | 16384 | 47293 | n/a | 29 msgs |
| call_009771 | optimizer | proposer | 44006 | 12288 | 13305 | n/a | — |
| call_009772 | optimizer | proposer | 40557 | 12288 | 13320 | n/a | — |
| call_009773 | optimizer | proposer | 23181 | 12288 | 12972 | n/a | — |
| call_009805 | optimizer | editpipe3-draft | 7704 | 16384 | 19782 | n/a | — |
| call_009806 | optimizer | editpipe3-draft | 7542 | 16384 | 46943 | n/a | — |
| call_009808 | optimizer | json-repair | 24717 | 16384 | 69544 | n/a | — |
| call_014089 | target | agent | 11044 | 16384 | 40499 | n/a | 13 msgs |
| call_014142 | optimizer | proposer | 40804 | 12288 | 54980 | n/a | — |
| call_014144 | optimizer | proposer | 77014 | 12288 | 13329 | n/a | — |
| call_015723 | target | agent | 16666 | 16384 | 33275 | n/a | 14 msgs |
| call_017703 | optimizer | proposer | 47412 | 12288 | 12867 | n/a | — |
| call_017720 | optimizer | editpipe3-draft | 13888 | 16384 | 17462 | n/a | — |
| call_017722 | optimizer | editpipe3-review | 18400 | 16384 | 41862 | n/a | — |
| call_019790 | target | agent | 16741 | 16384 | 47510 | n/a | 11 msgs |
| call_021281 | target | agent | 15614 | 16384 | 42295 | n/a | 9 msgs |
| call_022331 | optimizer | editpipe3-applier | 10899 | 16384 | 160271 | n/a | — |
| call_025400 | optimizer | interpret | 899 | 14843 | 71552 | **EMPTY** | 16 msgs |
| call_025402 | optimizer | interpret | 904 | 16384 | 72465 | **EMPTY** | 15 msgs |
| call_025403 | optimizer | interpret | 905 | 16384 | 76720 | **EMPTY** | 34 msgs |
| call_025404 | optimizer | interpret | 878 | 16384 | 56567 | **EMPTY** | 20 msgs |
| call_025405 | optimizer | interpret | 899 | 16384 | 56210 | **EMPTY** | 10 msgs |
| call_025406 | optimizer | interpret | 902 | 16384 | 58065 | **EMPTY** | 16 msgs |
| call_025407 | optimizer | interpret | 909 | 16384 | 72971 | **EMPTY** | 14 msgs |
| call_025408 | optimizer | interpret | 900 | 16384 | 57433 | **EMPTY** | 16 msgs |
| call_025409 | optimizer | interpret | 879 | 16384 | 63105 | **EMPTY** | 38 msgs |
| call_025410 | optimizer | interpret | 900 | 16384 | 73194 | **EMPTY** | 16 msgs |
| call_025411 | optimizer | interpret | 878 | 16384 | 74895 | **EMPTY** | 8 msgs |
| call_025412 | optimizer | interpret | 902 | 16384 | 58958 | **EMPTY** | 10 msgs |
| call_025413 | optimizer | interpret | 878 | 16384 | 63688 | **EMPTY** | 8 msgs |
| call_025414 | optimizer | interpret | 878 | 16384 | 62073 | **EMPTY** | 8 msgs |
| call_025415 | optimizer | interpret | 878 | 16384 | 65792 | **EMPTY** | 8 msgs |
| call_025416 | optimizer | interpret | 901 | 16384 | 65408 | **EMPTY** | 26 msgs |
| call_025417 | optimizer | interpret | 878 | 16384 | 79391 | **EMPTY** | 8 msgs |
| call_025418 | optimizer | interpret | 907 | 16384 | 71196 | **EMPTY** | 18 msgs |
| call_025419 | optimizer | interpret | 903 | 16384 | 60257 | **EMPTY** | 10 msgs |
| call_025438 | optimizer | interpret | 897 | 16384 | 74909 | **EMPTY** | 17 msgs |
| call_025439 | optimizer | interpret | 900 | 16384 | 87581 | **EMPTY** | 16 msgs |
| call_025440 | optimizer | interpret | 900 | 16384 | 71600 | **EMPTY** | 18 msgs |
| call_025441 | optimizer | interpret | 878 | 16384 | 62395 | **EMPTY** | 18 msgs |
| call_025442 | optimizer | interpret | 902 | 16384 | 74732 | **EMPTY** | 20 msgs |
| call_025443 | optimizer | interpret | 900 | 16384 | 46174 | **EMPTY** | 10 msgs |
| call_025444 | optimizer | interpret | 901 | 16384 | 69386 | **EMPTY** | 54 msgs |
| call_025445 | optimizer | interpret | 900 | 16384 | 76319 | **EMPTY** | 18 msgs |
| call_025446 | optimizer | interpret | 878 | 16384 | 62134 | **EMPTY** | 14 msgs |
| call_025447 | optimizer | interpret | 899 | 16384 | 55945 | **EMPTY** | 14 msgs |
| call_025448 | optimizer | interpret | 878 | 16384 | 64717 | **EMPTY** | 10 msgs |
| call_025449 | optimizer | interpret | 879 | 16384 | 66535 | **EMPTY** | 22 msgs |
| call_025450 | optimizer | interpret | 899 | 16384 | 74187 | **EMPTY** | 10 msgs |
| call_025451 | optimizer | interpret | 889 | 16384 | 69714 | **EMPTY** | 63 msgs |
| call_025452 | optimizer | interpret | 901 | 16384 | 72946 | **EMPTY** | 28 msgs |
| call_025453 | optimizer | interpret | 879 | 16384 | 64133 | **EMPTY** | 22 msgs |
| call_025454 | optimizer | interpret | 901 | 16384 | 54261 | **EMPTY** | 26 msgs |
| call_025455 | optimizer | interpret | 920 | 16384 | 63129 | **EMPTY** | 8 msgs |
| call_025456 | optimizer | interpret | 878 | 16384 | 59129 | **EMPTY** | 18 msgs |
| call_025457 | optimizer | interpret | 904 | 16384 | 69396 | **EMPTY** | 12 msgs |
| call_025458 | optimizer | interpret | 878 | 16384 | 64108 | **EMPTY** | 18 msgs |
| call_025459 | optimizer | interpret | 878 | 16384 | 61062 | **EMPTY** | 10 msgs |
| call_025460 | optimizer | interpret | 878 | 16384 | 88609 | **EMPTY** | 10 msgs |
| call_025461 | optimizer | interpret | 878 | 16384 | 68555 | **EMPTY** | 10 msgs |
| call_025462 | optimizer | interpret | 878 | 16384 | 69469 | **EMPTY** | 10 msgs |
| call_025463 | optimizer | interpret | 901 | 16384 | 73619 | **EMPTY** | 10 msgs |
| call_025464 | optimizer | interpret | 878 | 16384 | 59198 | **EMPTY** | 10 msgs |
| call_025465 | optimizer | interpret | 878 | 16384 | 64134 | **EMPTY** | 10 msgs |
| call_025466 | optimizer | interpret | 878 | 16384 | 78040 | **EMPTY** | 10 msgs |
| call_025467 | optimizer | interpret | 878 | 16384 | 68589 | **EMPTY** | 14 msgs |
| call_025498 | optimizer | interpret | 901 | 16384 | 65825 | **EMPTY** | 22 msgs |
| call_025499 | optimizer | interpret | 900 | 16384 | 54481 | **EMPTY** | 12 msgs |
| call_025500 | optimizer | interpret | 878 | 16384 | 67194 | **EMPTY** | 12 msgs |
| call_025501 | optimizer | interpret | 900 | 16384 | 62274 | **EMPTY** | 18 msgs |
| call_025502 | optimizer | interpret | 878 | 16384 | 64157 | **EMPTY** | 16 msgs |
| call_025503 | optimizer | interpret | 903 | 16384 | 65292 | **EMPTY** | 40 msgs |
| call_025504 | optimizer | interpret | 878 | 16384 | 61966 | **EMPTY** | 16 msgs |
| call_025505 | optimizer | interpret | 878 | 16384 | 63209 | **EMPTY** | 16 msgs |
| call_025506 | optimizer | interpret | 900 | 16384 | 145968 | **EMPTY** | 14 msgs |
| call_025507 | optimizer | interpret | 878 | 16384 | 64745 | **EMPTY** | 16 msgs |
| call_025508 | optimizer | interpret | 905 | 16384 | 59327 | **EMPTY** | 14 msgs |
| call_025509 | optimizer | interpret | 878 | 16384 | 60068 | **EMPTY** | 12 msgs |
| call_025510 | optimizer | interpret | 878 | 16384 | 71720 | **EMPTY** | 12 msgs |
| call_025511 | optimizer | interpret | 901 | 16384 | 70005 | **EMPTY** | 58 msgs |
| call_025512 | optimizer | interpret | 903 | 16384 | 57739 | **EMPTY** | 12 msgs |
| call_025513 | optimizer | interpret | 903 | 16384 | 65795 | **EMPTY** | 10 msgs |
| call_025536 | optimizer | other | 38722 | 16384 | 55559 | n/a | — |
| call_025537 | optimizer | json-repair | 55897 | 16384 | 55559 | n/a | — |
