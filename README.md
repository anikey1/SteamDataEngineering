# 🎮 Steam Hidden Gem Score

A data engineering pipeline that automatically identifies underrated games on Steam using a weighted scoring index. Built on AWS with a Medallion architecture (Bronze → Silver → Gold).

---

## 📌 What is it?

Most Steam users only discover games that are already popular. This project builds a **Hidden Gem Score (HGS)** — an objective, reproducible index that ranks games by their combination of perceived quality, low visibility, and accessible price. The result is an actionable list of overlooked titles that a marketing team could use to promote before they go mainstream.

### Score Formula

```
HGS = (positive_review_pct × 0.50) + (obscurity_score × 0.30) + (price_score × 0.20)
```

| Dimension | Weight | Logic |
|---|---|---|
| Perceived quality | 50% | % of positive reviews out of total |
| Obscurity | 30% | Inversely proportional to total review count |
| Accessible price | 20% | Inversely proportional to price (free-to-play supported) |

---

## 🏗️ Architecture

```
Steam API (public)
    │
    ▼
Python Extraction Script ──► S3 Bronze (raw JSONL)
    │                              │
EventBridge Scheduler              ▼
                           AWS Glue ETL + EvaluateDataQuality
                           (12 data quality rules)
                                   │
                                   ▼
                           S3 Silver (Parquet / Snappy)
                                   │
                                   ▼
                           HGS Score Calculation
                                   │
                                   ▼
                           RDS PostgreSQL (Gold) ──► Streamlit Dashboard
```

**Stack:**

- **Extraction:** Python, SteamSpy API, Steam Store API
- **Orchestration:** AWS EventBridge Scheduler
- **Storage:** Amazon S3 (Bronze / Silver / Gold layers)
- **ETL & Quality:** AWS Glue Visual ETL, `EvaluateDataQuality` (12 rules)
- **Database:** Amazon RDS PostgreSQL
- **Dashboard:** Streamlit
- **Monitoring:** Amazon CloudWatch
- **Security:** AWS IAM (least privilege roles)

---

## 📂 Project Structure

```
steam-hidden-gem-score/
├── extraction/
│   └── extract.py          # Pulls data from SteamSpy + Steam Store APIs
├── glue/
│   └── glue_job.py         # AWS Glue ETL script (Bronze → Silver)
├── scoring/
│   └── hgs_calc.py         # Hidden Gem Score formula + Gold layer load
├── dashboard/
│   └── app.py              # Streamlit dashboard
├── sql/
│   └── schema.sql          # RDS PostgreSQL table definitions
├── .gitignore
└── README.md
```

---

## 🔄 Pipeline Flow

1. **Extraction** — Python script queries SteamSpy and Steam Store APIs, writes raw JSONL to S3 Bronze partitioned by date (`bronze/YYYY-MM-DD/games.jsonl`).
2. **ETL + Quality** — AWS Glue job (`TEST-GLUE-JOB-DATA-QUALITY`) reads Bronze, applies 12 data quality rules (completeness, valid ranges, logical consistency), and writes validated data to S3 Silver in Parquet/Snappy format.
3. **Scoring** — HGS formula is applied to Silver data. Free-to-play games (price = 0) are fully supported and receive the maximum price score.
4. **Gold Load** — Scored data is loaded into RDS PostgreSQL via JDBC for querying and dashboard consumption.
5. **Visualization** — Streamlit dashboard allows filtering by genre, score range, price range, and extraction date.

---

## ✅ Data Quality Rules (AWS Glue EvaluateDataQuality)

12 rules across three categories:

- **Completeness** — `app_id`, `total_reviews`, `positive_reviews`, and `price` must be present.
- **Valid ranges** — Positive review percentage between 0 and 100; price ≥ 0.
- **Logical consistency** — `positive_reviews` ≤ `total_reviews`; games with 0 total reviews are excluded from scoring.

Only records that pass all 12 rules advance to Silver and Gold.

---

## 💡 Business Questions Answered

| # | Question | Layer |
|---|---|---|
| 1 | Which games have high perceived quality but low visibility? | Gold — HGS ranking |
| 2 | Which genres concentrate the most hidden gems? | Gold — aggregation by genre |
| 3 | Which games are rising in score between extractions? | Gold — historical comparison |

---

## ⚙️ Setup & Execution

### Prerequisites

- AWS account with S3, Glue, RDS, EventBridge, and CloudWatch configured
- Python 3.10+
- AWS CLI configured (`aws configure`)

### Environment variables

Create a `.env` file (never commit this):

```env
AWS_REGION=us-east-1
S3_BUCKET=steam-hgs-data-lake
RDS_HOST=your-rds-endpoint
RDS_DB=steam_hgs
RDS_USER=your-user
RDS_PASSWORD=your-password
```

### Run the extraction manually

```bash
python extraction/extract.py
```

### Trigger the Glue job manually

```bash
aws glue start-job-run --job-name TEST-GLUE-JOB-DATA-QUALITY
```

### Check job status

```bash
aws glue get-job-runs --job-name TEST-GLUE-JOB-DATA-QUALITY --max-results 5
```

### Run the dashboard

```bash
cd dashboard
pip install -r requirements.txt
streamlit run app.py
```

---

## 👥 Team

**Equipo 3 — Ingeniería de Datos**  
Facultad de Ingeniería, UNAM — 2026

---

## 📄 License

Academic project. Not affiliated with Valve Corporation or Steam.
