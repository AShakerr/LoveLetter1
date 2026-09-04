# Seasonality evidence

This file governs `f_season` in `desk/score.py`. The rule in the brief: no seasonality logic is added without an entry here, with a source and a sample period. Two independent research passes (4 Sep 2026) produced the verdicts below; where they disagreed on a number, both are shown.

## What the score is allowed to use

Only these two rules earn points. Everything else in this file scores 0.

1. **Nov–Dec concentration.** +3 if the scoring date is in November or December and the instrument is a broad equity index ETF or a cyclical sector.
   Basis: Q4 averages +4.1–4.2% and is positive ~80% of years (S&P 500, 1950–2025, LPL and Carson). November +1.5% / 68% hit rate; December +1.4% / 74% hit rate, the highest of any month (1950–2025, Simianx / YCharts). Q4's entire edge sits in Nov–Dec; October is mid-pack (+0.9%, 61%).
   Sources: https://www.cnbc.com/2023/10/12/what-fourth-quarter-market-history-means-for-your-money.html · https://www.simianx.ai/stories/sp-500-seasonality-best-worst-months-1950-2026 · https://www.carsongroup.com/insights/blog/7-things-to-know-about-historically-strong-fourth-quarter-2/

2. **Cyclical tilt.** +2 more if the instrument is Industrials, Materials, Consumer Discretionary or Technology.
   Basis: Jacobsen & Visaltanachoti, *Financial Review* 44(3), 2009, US sectors 1926–2006: Nov–Apr beats May–Oct in more than two-thirds of sectors, strongest in production/cyclical sectors, almost absent in consumer-consumption sectors. CFRA/Stovall, Apr 1990–Apr 2019: Nov–Apr overweight of Discretionary, Industrials, IT and Materials averaged 6.9% per six months vs S&P 4.6%. Antonacci, Jan 1973–Jul 2020: Nov–Apr excess return Materials +5.70%, Discretionary +4.27%, Industrials +3.21%.
   Sources: https://ideas.repec.org/a/bla/finrev/v44y2009i3p437-459.html · https://www.fidelity.com/bin-public/060_www_fidelity_com/documents/learning-center/Presentation_Sector%20rotation.pdf · https://www.optimalmomentum.com/seasonality-factor/

Supporting, not scored: the global Halloween effect. Jacobsen & Zhang, *JIMF* 2020, 108 countries, 319 years: Nov–Apr beats May–Oct by 4.52pp/yr, t = 9.69, out-of-sample 1998–2011 positive in 37 of 37 original countries. This is the only calendar effect that clears the Harvey–Liu–Zhu t > 3.0 bar. But for the **US alone** the coefficient is 1.67pp at the 10% level, and Maberly & Pierce (2004) showed the US result rests on October 1987 and August 1998. That is why the weight is 10 and not more.

## What is explicitly excluded

| Claim | Verdict | Why |
|---|---|---|
| September is a bad month (−0.7% 1950–2025, −1.2% from 1928) | Fails multiplicity correction | Only negative-mean month, but a max-of-12 statistic. Winton showed October's volatility p-value goes from 0.7% to 8% once all 12 months are tested; the same applies here. The −1.2% figure leans on 1929–1937. |
| Santa Claus rally (+1.3%, 76–78% hit, last 5 + first 2 trading days) | Drift is real, tiny sample, no forecast value | n ≈ 76 non-overlapping weeks. As a predictor of the following year it fails ~65% of the time (Fisher: no-rally years preceded a down year only 35% of the time). |
| Midterm-year Q4 rally / "19 for 19 since 1950" | Thin | n = 19 overlapping windows inside one secular bull market. U.S. Bank's test over 31 elections (1900–2025) finds differences "not large or consistent enough to establish a reliable election effect." 2026 also did not follow the template (no weak Q1–Q3). |
| January effect / tax-loss bounce | Decayed | Real in small caps pre-1988 (Haug & Hirschey 2006); has migrated into late December (Dzhabarov & Ziemba 2011); Quantpedia: transaction costs make it untradable. |
| Window dressing (Lakonishok, Shleifer, Thaler & Vishny 1991; Ng & Wang 2004) | Mechanism only | Institutional Q4 loser-selling is documented at the trade level; price effect confined to illiquid small caps we do not trade. |
| Gold is strong in Q4 | Folklore | World Gold Council 1971–2023: significant months are January (+1.79%) and late summer; no Q4 month is significant. Practitioner sources contradict each other on which months are strong. |
| Bitcoin "Uptober" / Q4 | No usable evidence | n ≈ 12–13 Q4s; 2013 (+479%) carries the mean; three sources report averages of 27.7%, 80.6% and 85% for the same statistic; Q4 2025 was −23%; halving cycle confounds. |
| October "bear killer" | Folklore | 12 post-war bear bottoms in October is what you expect by chance with 12 months to choose from; not an ex-ante rule. |
| Crude weak Nov–Dec | Weak tendency | CXO, WTI 1986–2020: monthly σ 6.7–14.2% vs 0.61% mean; noise swamps it. |

## The counter-literature this file is built on

- Sullivan, Timmermann & White (2001), "Dangers of data mining: the case of calendar effects in stock returns," *Journal of Econometrics* 105(1). 100 years of daily DJIA, ~9,450 calendar rules: individually significant, collectively not. https://ideas.repec.org/a/eee/econom/v105y2001i1p249-286.html
- Harvey, Liu & Zhu (2016), *Review of Financial Studies* 29(1). Given the volume of mined factors, a finding needs t > 3.0. https://www.nber.org/papers/w20592
- McLean & Pontiff (2016), *Journal of Finance* 71(1). 97 predictors: 26% out-of-sample decay, 58% post-publication decay. https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.12365
- Hansen, Lunde & Nason (2005), FRB Atlanta WP 2005-02. Bootstrap across 10 countries: end-of-year effects predominate but have diminished since the late 1980s except in small caps. https://ideas.repec.org/p/fip/fedawp/2005-02.html
- Plastun, Sibande, Gupta & Wohar (2019), DJIA 1900–2018: calendar anomalies peaked mid-century, "since the 1980s all calendar anomalies disappeared" in trading-simulation terms. https://ideas.repec.org/p/pre/wpaper/201902.html
- Maberly & Pierce (2004), *Econ Journal Watch* 1(1): US Halloween effect driven by two outlier months. https://ideas.repec.org/a/ejw/journl/v1y2004i1p29-46.html

## How to add a rule

Append a numbered entry under "What the score is allowed to use" with: the exact rule, the statistic, the sample period, a peer-reviewed or primary source, and a sentence on why it survives multiple-testing correction. Then, and only then, change `desk/score.py`. Raising the 10% weight requires the same, plus a note in this section explaining what changed in the evidence.
