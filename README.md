# 🤖 Autonomous Vendor Negotiation & Risk Matrix

A **production-grade multi-agent system** for automated supply chain risk assessment, built with **LangGraph**, **LangChain**, **OpenAI**, **Pinecone**, and **FastAPI**.

---

## Architecture

```
POST /webhook/vendor-log
        │
        ▼
┌───────────────────┐
│  researcher_node  │  ← Retrieves compliance policies (Pinecone)
└────────┬──────────┘
         │
         ▼
┌───────────────────┐
│  classifier_node  │  ← LLM risk classification (ChatOpenAI)
└────────┬──────────┘
         │
   ┌─────┴──────┐
   │            │
  HIGH/     LOW/MEDIUM
 CRITICAL       │
   │            ▼
   ▼           END
┌──────────────────┐
│   action_node    │  ← Executes vendor pause (Procurement API)
└──────────────────┘
```

### Module Breakdown

| File | Responsibility |
|---|---|
| `app/config.py` | Loads & validates environment variables via `pydantic-settings` |
| `app/models.py` | Pydantic `RiskClassification` model used for structured LLM output |
| `app/state.py` | LangGraph `TypedDict` state shared across all nodes |
| `app/tools.py` | `@tool`-decorated functions: Pinecone retrieval & procurement API call |
| `app/agents.py` | Node functions: `researcher_node`, `classifier_node`, `action_node` |
| `app/graph.py` | `StateGraph` definition, conditional routing, compiled graph |
| `app/main.py` | FastAPI server exposing `/webhook/vendor-log` |

---

## Prerequisites

- Python `>= 3.11`
- A Google AI Studio API key (`gemini-2.0-flash` or better) — get one at https://aistudio.google.com/apikey
- A Pinecone account and index (optional — tools are mocked by default)

---

## Setup

```bash
# 1. Clone and enter the project
cd multi-agent-project

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -e ".[dev]"

# 4. Configure environment variables
cp .env.example .env
# Edit .env and fill in your GEMINI_API_KEY (and optionally PINECONE_API_KEY)
```

---

## Running the Server

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

The server will be available at `http://localhost:8000`.

Interactive API docs: `http://localhost:8000/docs`

---

## Usage

### Trigger a vendor risk assessment

```bash
curl -X POST http://localhost:8000/webhook/vendor-log \
  -H "Content-Type: application/json" \
  -d '{
    "vendor_id": "V-1234",
    "log_text": "Vendor missed delivery deadline by 15 days and unilaterally increased contract prices by 40% without notice. Multiple quality non-conformances detected in last shipment."
  }'
```

### Example Response

```json
{
  "vendor_id": "V-1234",
  "risk_classification": {
    "risk_level": "HIGH",
    "confidence_score": 0.92,
    "risk_factors": [
      "15-day delivery delay",
      "40% unauthorized price increase",
      "Quality non-conformances in shipment"
    ],
    "recommended_action": "Immediate vendor pause and contract review",
    "reasoning": "Multiple severe compliance violations detected..."
  },
  "action_taken": "VENDOR_PAUSED | txn_id=mock-txn-V-1234-...",
  "retrieved_policies_count": 3
}
```

### Health Check

```bash
curl http://localhost:8000/health
```

---

## Extending with Real Pinecone

The `tools.py` file contains `TODO` comments marking where to wire in the real Pinecone client:

```python
# TODO: Replace mock with real Pinecone retrieval
# from langchain_pinecone import PineconeVectorStore
# from langchain_google_genai import GoogleGenerativeAIEmbeddings
# vectorstore = PineconeVectorStore(index_name=settings.pinecone_index_name, embedding=GoogleGenerativeAIEmbeddings(model="models/embedding-001"))
# results = vectorstore.similarity_search(query_text, k=5)
# return [doc.page_content for doc in results]
```

---

## License

MIT
