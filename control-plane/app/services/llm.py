import os
import json
import logging
import httpx
from typing import Optional

logger = logging.getLogger(__name__)

class VertexAIClient:
    def __init__(self, region: str = "us-central1", project_id: Optional[str] = None):
        self.region = region
        self.project_id = project_id or os.getenv("GCP_PROJECT") or "mock-project"
        self.model = "gemini-1.5-pro"

    def generate_edits(self, intent: str, files_context: str, system_instruction: Optional[str] = None) -> dict:
        """Sends the prompt to Vertex AI Gemini model to get code edits in structured JSON."""
        if os.getenv("AOS_ENV") == "test":
            logger.info("[TEST MODE] Returning mock edits for LLM request")
            return {
                "explanation": "Simulated edits for testing",
                "edits": [
                    {
                        "path": "src/App.js",
                        "action": "modify",
                        "content": "function App() {\n  return <Hero color=\"blue\" />;\n}\n"
                    }
                ]
            }

        import google.auth
        import google.auth.transport.requests

        try:
            credentials, resolved_project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            auth_req = google.auth.transport.requests.Request()
            credentials.refresh(auth_req)
            token = credentials.token
            project = self.project_id or resolved_project
        except Exception as e:
            logger.error(f"Failed to obtain GCP credentials: {e}")
            raise RuntimeError(f"GCP Authentication failed: {e}")

        url = f"https://{self.region}-aiplatform.googleapis.com/v1/projects/{project}/locations/{self.region}/publishers/google/models/{self.model}:generateContent"
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": f"Change request: {intent}\n\nCodebase Context:\n{files_context}"
                        }
                    ]
                }
            ],
            "systemInstruction": {
                "parts": [
                    {
                        "text": system_instruction or "You are a professional software engineer. Generate codebase edits in JSON matching the requested responseSchema. Maintain existing indentation and style."
                    }
                ]
            },
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "explanation": {
                            "type": "STRING"
                        },
                        "edits": {
                            "type": "ARRAY",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "path": {
                                        "type": "STRING"
                                    },
                                    "action": {
                                        "type": "STRING",
                                        "enum": ["modify", "create", "delete"]
                                    },
                                    "content": {
                                        "type": "STRING"
                                    }
                                },
                                "required": ["path", "action", "content"]
                            }
                        }
                    },
                    "required": ["explanation", "edits"]
                }
            }
        }

        with httpx.Client() as client:
            resp = client.post(url, json=payload, headers=headers, timeout=60.0)
            if resp.status_code != 200:
                logger.error(f"Vertex AI API returned error: {resp.status_code} - {resp.text}")
                raise RuntimeError(f"Vertex AI request failed: {resp.text}")
            
            data = resp.json()
            try:
                # Parse text response from the choices block
                text_content = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text_content)
            except (KeyError, IndexError, ValueError) as e:
                logger.error(f"Failed to parse model response payload: {e}. Raw response: {data}")
                raise RuntimeError("Invalid model response format")

    def generate_recommendations(self, data_context: str, system_instruction: str) -> dict:
        """Sends performance data to Vertex AI to get structured optimization recommendations."""
        if os.getenv("AOS_ENV") == "test":
            logger.info("[TEST MODE] Returning mock recommendations for LLM request")
            source_camp = "camp-low-performance"
            target_camp = "camp-high-performance"
            try:
                import json
                camps = json.loads(data_context)
                if isinstance(camps, list) and len(camps) >= 2:
                    ids = [c["id"] for c in camps]
                    if "camp-g1" in ids and "camp-m1" in ids:
                        source_camp = "camp-g1"
                        target_camp = "camp-m1"
                    else:
                        source_camp = camps[0]["id"]
                        target_camp = camps[1]["id"]
            except Exception:
                pass
            return {
                "recommendations": [
                    {
                        "action": "grow.budget.reallocate",
                        "params": {
                            "source_campaign_id": source_camp,
                            "target_campaign_id": target_camp,
                            "amount_minor": 100000
                        },
                        "explanation": f"Reallocate 1,000 INR from low ROI campaign ({source_camp}) to high ROI campaign ({target_camp}) based on performance trends.",
                        "impact": 1,
                        "reversibility": "REVERSIBLE"
                    }
                ]
            }

        import google.auth
        import google.auth.transport.requests

        try:
            credentials, resolved_project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            auth_req = google.auth.transport.requests.Request()
            credentials.refresh(auth_req)
            token = credentials.token
            project = self.project_id or resolved_project
        except Exception as e:
            logger.error(f"Failed to obtain GCP credentials: {e}")
            raise RuntimeError(f"GCP Authentication failed: {e}")

        url = f"https://{self.region}-aiplatform.googleapis.com/v1/projects/{project}/locations/{self.region}/publishers/google/models/{self.model}:generateContent"
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": f"Performance Data:\n{data_context}"
                        }
                    ]
                }
            ],
            "systemInstruction": {
                "parts": [
                    {
                        "text": system_instruction
                    }
                ]
            },
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "recommendations": {
                            "type": "ARRAY",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "action": {
                                        "type": "STRING",
                                        "enum": ["grow.bid.adjust", "grow.budget.reallocate", "grow.campaign.pause"]
                                    },
                                    "params": {
                                        "type": "OBJECT",
                                        "properties": {
                                            "campaign_id": {"type": "STRING"},
                                            "source_campaign_id": {"type": "STRING"},
                                            "target_campaign_id": {"type": "STRING"},
                                            "amount_minor": {"type": "INTEGER"},
                                            "new_bid_minor": {"type": "INTEGER"}
                                        }
                                    },
                                    "explanation": {
                                        "type": "STRING"
                                    },
                                    "impact": {
                                        "type": "INTEGER"
                                    },
                                    "reversibility": {
                                        "type": "STRING",
                                        "enum": ["REVERSIBLE", "COMPENSATABLE", "IRREVERSIBLE"]
                                    }
                                },
                                "required": ["action", "params", "explanation", "impact", "reversibility"]
                            }
                        }
                    },
                    "required": ["recommendations"]
                }
            }
        }

        with httpx.Client() as client:
            resp = client.post(url, json=payload, headers=headers, timeout=60.0)
            if resp.status_code != 200:
                logger.error(f"Vertex AI API returned error: {resp.status_code} - {resp.text}")
                raise RuntimeError(f"Vertex AI request failed: {resp.text}")
            
            data = resp.json()
            try:
                text_content = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text_content)
            except (KeyError, IndexError, ValueError) as e:
                logger.error(f"Failed to parse model response payload: {e}. Raw response: {data}")
                raise RuntimeError("Invalid model response format")

    def analyze_security_diff(self, diff: str, system_instruction: str) -> dict:
        """Sends the git diff to Vertex AI Gemini model to analyze for security risks."""
        if os.getenv("AOS_ENV") == "test":
            logger.info("[TEST MODE] Returning mock security analysis")
            passed = True
            violations = []
            risk_score = 1
            report = "No issues found in test mode."
            
            # Simple triggers for testing failures
            if "AWS_SECRET_ACCESS_KEY" in diff or "secret_key" in diff:
                passed = False
                violations = ["Found hardcoded AWS_SECRET_ACCESS_KEY in diff"]
                risk_score = 5
                report = "CRITICAL: Hardcoded credentials detected in the changes."
            elif "eval(" in diff:
                passed = False
                violations = ["Found eval() usage in javascript"]
                risk_score = 4
                report = "HIGH: Remote code execution risk via eval()."
                
            return {
                "passed": passed,
                "violations": violations,
                "risk_score": risk_score,
                "detailed_report": report
            }

        import google.auth
        import google.auth.transport.requests

        try:
            credentials, resolved_project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            auth_req = google.auth.transport.requests.Request()
            credentials.refresh(auth_req)
            token = credentials.token
            project = self.project_id or resolved_project
        except Exception as e:
            logger.error(f"Failed to obtain GCP credentials: {e}")
            raise RuntimeError(f"GCP Authentication failed: {e}")

        url = f"https://{self.region}-aiplatform.googleapis.com/v1/projects/{project}/locations/{self.region}/publishers/google/models/{self.model}:generateContent"
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": f"Proposed Changes Git Diff:\n{diff}"
                        }
                    ]
                }
            ],
            "systemInstruction": {
                "parts": [
                    {
                        "text": system_instruction
                    }
                ]
            },
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "passed": {
                            "type": "BOOLEAN"
                        },
                        "violations": {
                            "type": "ARRAY",
                            "items": {
                                "type": "STRING"
                            }
                        },
                        "risk_score": {
                            "type": "INTEGER"
                        },
                        "detailed_report": {
                            "type": "STRING"
                        }
                    },
                    "required": ["passed", "violations", "risk_score", "detailed_report"]
                }
            }
        }

        with httpx.Client() as client:
            resp = client.post(url, json=payload, headers=headers, timeout=60.0)
            if resp.status_code != 200:
                logger.error(f"Vertex AI API returned error: {resp.status_code} - {resp.text}")
                raise RuntimeError(f"Vertex AI request failed: {resp.text}")
            
            data = resp.json()
            try:
                text_content = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text_content)
            except (KeyError, IndexError, ValueError) as e:
                logger.error(f"Failed to parse model response payload: {e}. Raw response: {data}")
                raise RuntimeError("Invalid model response format")

    def analyze_citations(self, citation_data: str, system_instruction: str) -> dict:
        """Sends citation crawling data to Vertex AI Gemini model to analyze for AEO/GEO gap optimization."""
        if os.getenv("AOS_ENV") == "test":
            logger.info("[TEST MODE] Returning mock citation gap analysis")
            return {
                "gap_analysis": "Mock Gap Analysis: Brand has 0 citations compared to competitors.",
                "recommendations": ["Create llms.txt to guide AI crawler discovery.", "Optimize robots.txt to allow Googlebot-Extended."],
                "propose_llms_txt": True,
                "llms_txt_content": "# Brand Profile\nWe are a premium brand selling high quality items.\n\n## Products\n- Awesome shoes\n- Cool shirts"
            }

        import google.auth
        import google.auth.transport.requests

        try:
            credentials, resolved_project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            auth_req = google.auth.transport.requests.Request()
            credentials.refresh(auth_req)
            token = credentials.token
            project = self.project_id or resolved_project
        except Exception as e:
            logger.error(f"Failed to obtain GCP credentials: {e}")
            raise RuntimeError(f"GCP Authentication failed: {e}")

        url = f"https://{self.region}-aiplatform.googleapis.com/v1/projects/{project}/locations/{self.region}/publishers/google/models/{self.model}:generateContent"
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": f"Brand Citation Crawl Findings:\n{citation_data}"
                        }
                    ]
                }
            ],
            "systemInstruction": {
                "parts": [
                    {
                        "text": system_instruction
                    }
                ]
            },
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "gap_analysis": {
                            "type": "STRING"
                        },
                        "recommendations": {
                            "type": "ARRAY",
                            "items": {
                                "type": "STRING"
                            }
                        },
                        "propose_llms_txt": {
                            "type": "BOOLEAN"
                        },
                        "llms_txt_content": {
                            "type": "STRING"
                        }
                    },
                    "required": ["gap_analysis", "recommendations", "propose_llms_txt", "llms_txt_content"]
                }
            }
        }

        with httpx.Client() as client:
            resp = client.post(url, json=payload, headers=headers, timeout=60.0)
            if resp.status_code != 200:
                logger.error(f"Vertex AI API returned error: {resp.status_code} - {resp.text}")
                raise RuntimeError(f"Vertex AI request failed: {resp.text}")
            
            data = resp.json()
            try:
                text_content = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text_content)
            except (KeyError, IndexError, ValueError) as e:
                logger.error(f"Failed to parse model response payload: {e}. Raw response: {data}")
                raise RuntimeError("Invalid model response format")

    def analyze_design_diff(self, diff: str, system_instruction: str) -> dict:
        """Sends the git diff to Vertex AI Gemini model to analyze for UI/UX brand compliance."""
        if os.getenv("AOS_ENV") == "test":
            logger.info("[TEST MODE] Returning mock design analysis")
            passed = True
            violations = []
            score = 5
            report = "Design aligns with brand guidelines."
            
            if "color=\"pink\"" in diff or "pink" in diff:
                passed = False
                violations = ["Brand colors only permit primary blue and dark grey. Pink is disallowed."]
                score = 2
                report = "FAILED: Disallowed color usage."
                
            return {
                "passed": passed,
                "violations": violations,
                "score": score,
                "detailed_report": report
            }

        import google.auth
        import google.auth.transport.requests

        try:
            credentials, resolved_project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            auth_req = google.auth.transport.requests.Request()
            credentials.refresh(auth_req)
            token = credentials.token
            project = self.project_id or resolved_project
        except Exception as e:
            logger.error(f"Failed to obtain GCP credentials: {e}")
            raise RuntimeError(f"GCP Authentication failed: {e}")

        url = f"https://{self.region}-aiplatform.googleapis.com/v1/projects/{project}/locations/{self.region}/publishers/google/models/{self.model}:generateContent"
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": f"Proposed Changes Git Diff:\n{diff}"
                        }
                    ]
                }
            ],
            "systemInstruction": {
                "parts": [
                    {
                        "text": system_instruction
                    }
                ]
            },
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "passed": {
                            "type": "BOOLEAN"
                        },
                        "violations": {
                            "type": "ARRAY",
                            "items": {
                                "type": "STRING"
                            }
                        },
                        "score": {
                            "type": "INTEGER"
                        },
                        "detailed_report": {
                            "type": "STRING"
                        }
                    },
                    "required": ["passed", "violations", "score", "detailed_report"]
                }
            }
        }

        with httpx.Client() as client:
            resp = client.post(url, json=payload, headers=headers, timeout=60.0)
            if resp.status_code != 200:
                logger.error(f"Vertex AI API returned error: {resp.status_code} - {resp.text}")
                raise RuntimeError(f"Vertex AI request failed: {resp.text}")
            
            data = resp.json()
            try:
                text_content = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text_content)
            except (KeyError, IndexError, ValueError) as e:
                logger.error(f"Failed to parse model response payload: {e}. Raw response: {data}")
                raise RuntimeError("Invalid model response format")

    def analyze_pricing(self, products_json: str, system_instruction: str) -> dict:
        """Analyzes product pricing and margins using Vertex AI Gemini."""
        if os.getenv("AOS_ENV") == "test":
            logger.info("[TEST MODE] Returning mock pricing recommendations")
            return {
                "pricing_report": "Competitors are selling 'AOS-T-SHIRT' at 999 INR. Our current price is 799 INR (cost: 300 INR). We can optimize margin.",
                "recommendations": [
                    {
                        "sku": "AOS-T-SHIRT-M",
                        "current_price": 799,
                        "recommended_price": 899,
                        "reason": "Align with market pricing while preserving competitive discount"
                    }
                ],
                "propose_price_update": True
            }

        import google.auth
        import google.auth.transport.requests

        try:
            credentials, resolved_project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            auth_req = google.auth.transport.requests.Request()
            credentials.refresh(auth_req)
            token = credentials.token
            project = self.project_id or resolved_project
        except Exception as e:
            logger.error(f"Failed to obtain GCP credentials: {e}")
            raise RuntimeError(f"GCP Authentication failed: {e}")

        url = f"https://{self.region}-aiplatform.googleapis.com/v1/projects/{project}/locations/{self.region}/publishers/google/models/{self.model}:generateContent"
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": f"Product Pricing Catalog and Costs:\n{products_json}"
                        }
                    ]
                }
            ],
            "systemInstruction": {
                "parts": [
                    {
                        "text": system_instruction
                    }
                ]
            },
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "pricing_report": {
                            "type": "STRING"
                        },
                        "recommendations": {
                            "type": "ARRAY",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "sku": {
                                        "type": "STRING"
                                    },
                                    "current_price": {
                                        "type": "INTEGER"
                                    },
                                    "recommended_price": {
                                        "type": "INTEGER"
                                    },
                                    "reason": {
                                        "type": "STRING"
                                    }
                                },
                                "required": ["sku", "current_price", "recommended_price", "reason"]
                            }
                        },
                        "propose_price_update": {
                            "type": "BOOLEAN"
                        }
                    },
                    "required": ["pricing_report", "recommendations", "propose_price_update"]
                }
            }
        }

        with httpx.Client() as client:
            resp = client.post(url, json=payload, headers=headers, timeout=60.0)
            if resp.status_code != 200:
                logger.error(f"Vertex AI API returned error: {resp.status_code} - {resp.text}")
                raise RuntimeError(f"Vertex AI request failed: {resp.text}")
            
            data = resp.json()
            try:
                text_content = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text_content)
            except (KeyError, IndexError, ValueError) as e:
                logger.error(f"Failed to parse model response payload: {e}. Raw response: {data}")
                raise RuntimeError("Invalid model response format")

    def analyze_performance(self, campaigns_json: str, system_instruction: str) -> dict:
        """Analyzes marketing campaign performance metrics using Vertex AI Gemini."""
        if os.getenv("AOS_ENV") == "test":
            logger.info("[TEST MODE] Returning mock PPC adjustments")
            import json
            pauses = []
            if "fail" in campaigns_json.lower():
                pauses.append({"campaign_id": "camp-google-fail", "reason": "Extremely low ROAS"})
            return {
                "propose_pauses": pauses,
                "recommendations": [
                    {
                        "type": "bid_adjustment",
                        "campaign_id": "camp-google",
                        "recommended_bid_minor": 18000,
                        "current_bid_minor": 15000,
                        "reason": "Strong performance, CTR is above 3%"
                    },
                    {
                        "type": "budget_reallocation",
                        "source_campaign_id": "camp-meta",
                        "target_campaign_id": "camp-google",
                        "transfer_amount_minor": 2000000,
                        "reason": "Higher conversion efficiency on Google Ads"
                    }
                ]
            }

        import google.auth
        import google.auth.transport.requests

        try:
            credentials, resolved_project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            auth_req = google.auth.transport.requests.Request()
            credentials.refresh(auth_req)
            token = credentials.token
            project = self.project_id or resolved_project
        except Exception as e:
            logger.error(f"Failed to obtain GCP credentials: {e}")
            raise RuntimeError(f"GCP Authentication failed: {e}")

        url = f"https://{self.region}-aiplatform.googleapis.com/v1/projects/{project}/locations/{self.region}/publishers/google/models/{self.model}:generateContent"
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": f"PPC Campaigns and Performance Metrics:\n{campaigns_json}"
                        }
                    ]
                }
            ],
            "systemInstruction": {
                "parts": [
                    {
                        "text": system_instruction
                    }
                ]
            },
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "propose_pauses": {
                            "type": "ARRAY",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "campaign_id": {"type": "STRING"},
                                    "reason": {"type": "STRING"}
                                },
                                "required": ["campaign_id", "reason"]
                            }
                        },
                        "recommendations": {
                            "type": "ARRAY",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "type": {"type": "STRING"},
                                    "campaign_id": {"type": "STRING"},
                                    "recommended_bid_minor": {"type": "INTEGER"},
                                    "current_bid_minor": {"type": "INTEGER"},
                                    "source_campaign_id": {"type": "STRING"},
                                    "target_campaign_id": {"type": "STRING"},
                                    "transfer_amount_minor": {"type": "INTEGER"},
                                    "reason": {"type": "STRING"}
                                },
                                "required": ["type", "reason"]
                            }
                        }
                    },
                    "required": ["propose_pauses", "recommendations"]
                }
            }
        }

        with httpx.Client() as client:
            resp = client.post(url, json=payload, headers=headers, timeout=60.0)
            if resp.status_code != 200:
                logger.error(f"Vertex AI API returned error: {resp.status_code} - {resp.text}")
                raise RuntimeError(f"Vertex AI request failed: {resp.text}")
            
            data = resp.json()
            try:
                text_content = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text_content)
            except (KeyError, IndexError, ValueError) as e:
                logger.error(f"Failed to parse model response payload: {e}. Raw response: {data}")
                raise RuntimeError("Invalid model response format")

    def analyze_accessibility(self, html_content: str, system_instruction: str) -> dict:
        """Analyzes page HTML for accessibility compliance (WCAG 2.2 AA)."""
        if os.getenv("AOS_ENV") == "test":
            logger.info("[TEST MODE] Returning mock accessibility report")
            if "fail-a11y" in html_content:
                return {
                    "passed": False,
                    "violations": ["Image missing alt text (WCAG 1.1.1)", "Contrast ratio below 4.5:1 (WCAG 1.4.3)"],
                    "score_percent": 60,
                    "report": "Mock accessibility review: detected multiple failures."
                }
            return {
                "passed": True,
                "violations": [],
                "score_percent": 95,
                "report": "Mock accessibility review: all elements compliant."
            }

        import google.auth
        import google.auth.transport.requests

        try:
            credentials, resolved_project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            auth_req = google.auth.transport.requests.Request()
            credentials.refresh(auth_req)
            token = credentials.token
            project = self.project_id or resolved_project
        except Exception as e:
            logger.error(f"Failed to obtain GCP credentials: {e}")
            raise RuntimeError(f"GCP Authentication failed: {e}")

        url = f"https://{self.region}-aiplatform.googleapis.com/v1/projects/{project}/locations/{self.region}/publishers/google/models/{self.model}:generateContent"
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": f"Page HTML to Audit:\n{html_content}"
                        }
                    ]
                }
            ],
            "systemInstruction": {
                "parts": [
                    {
                        "text": system_instruction
                    }
                ]
            },
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "passed": {"type": "BOOLEAN"},
                        "violations": {
                            "type": "ARRAY",
                            "items": {"type": "STRING"}
                        },
                        "score_percent": {"type": "INTEGER"},
                        "report": {"type": "STRING"}
                    },
                    "required": ["passed", "violations", "score_percent", "report"]
                }
            }
        }

        with httpx.Client() as client:
            resp = client.post(url, json=payload, headers=headers, timeout=60.0)
            if resp.status_code != 200:
                logger.error(f"Vertex AI API returned error: {resp.status_code} - {resp.text}")
                raise RuntimeError(f"Vertex AI request failed: {resp.text}")
            
            data = resp.json()
            try:
                text_content = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text_content)
            except (KeyError, IndexError, ValueError) as e:
                logger.error(f"Failed to parse model response payload: {e}. Raw response: {data}")
                raise RuntimeError("Invalid model response format")

    def analyze_performance_markup(self, html_content: str, system_instruction: str) -> dict:
        """Analyzes page HTML for performance/markup efficiency rules."""
        if os.getenv("AOS_ENV") == "test":
            logger.info("[TEST MODE] Returning mock performance markup report")
            if "fail-perf" in html_content:
                return {
                    "passed": False,
                    "violations": ["Render-blocking scripts in head", "Uncompressed heavy inline CSS style block"],
                    "score_percent": 55,
                    "report": "Mock performance review: detected script blocking bottlenecks."
                }
            return {
                "passed": True,
                "violations": [],
                "score_percent": 90,
                "report": "Mock performance review: HTML structure optimal."
            }

        import google.auth
        import google.auth.transport.requests

        try:
            credentials, resolved_project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            auth_req = google.auth.transport.requests.Request()
            credentials.refresh(auth_req)
            token = credentials.token
            project = self.project_id or resolved_project
        except Exception as e:
            logger.error(f"Failed to obtain GCP credentials: {e}")
            raise RuntimeError(f"GCP Authentication failed: {e}")

        url = f"https://{self.region}-aiplatform.googleapis.com/v1/projects/{project}/locations/{self.region}/publishers/google/models/{self.model}:generateContent"
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": f"Page HTML to Audit:\n{html_content}"
                        }
                    ]
                }
            ],
            "systemInstruction": {
                "parts": [
                    {
                        "text": system_instruction
                    }
                ]
            },
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "passed": {"type": "BOOLEAN"},
                        "violations": {
                            "type": "ARRAY",
                            "items": {"type": "STRING"}
                        },
                        "score_percent": {"type": "INTEGER"},
                        "report": {"type": "STRING"}
                    },
                    "required": ["passed", "violations", "score_percent", "report"]
                }
            }
        }

        with httpx.Client() as client:
            resp = client.post(url, json=payload, headers=headers, timeout=60.0)
            if resp.status_code != 200:
                logger.error(f"Vertex AI API returned error: {resp.status_code} - {resp.text}")
                raise RuntimeError(f"Vertex AI request failed: {resp.text}")
            
            data = resp.json()
            try:
                text_content = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text_content)
            except (KeyError, IndexError, ValueError) as e:
                logger.error(f"Failed to parse model response payload: {e}. Raw response: {data}")
                raise RuntimeError("Invalid model response format")

    def generate_design_blueprint(self, intent: str, system_instruction: str) -> dict:
        """Generates visual themes, wireframes, and image prompts for a website makeover."""
        if os.getenv("AOS_ENV") == "test":
            logger.info("[TEST MODE] Returning mock design blueprint")
            return {
                "ux_wireframes": "Mock UX Spec: Header (Nav), Hero Zone (CTA), Product Showcase, Footer.",
                "css_theme": {
                    "primary": "#3F51B5",
                    "secondary": "#FF4081",
                    "fonts": ["Poppins", "Roboto"]
                },
                "image_prompts": [
                    "Ultra-clean minimalist banner showcasing organic skin care products on stone podiums, warm sunlight, 8k",
                    "Close-up flatlay of glass dropper bottles, neutral pastel background, professional product photography"
                ]
            }

        import google.auth
        import google.auth.transport.requests

        try:
            credentials, resolved_project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            auth_req = google.auth.transport.requests.Request()
            credentials.refresh(auth_req)
            token = credentials.token
            project = self.project_id or resolved_project
        except Exception as e:
            logger.error(f"Failed to obtain GCP credentials: {e}")
            raise RuntimeError(f"GCP Authentication failed: {e}")

        url = f"https://{self.region}-aiplatform.googleapis.com/v1/projects/{project}/locations/{self.region}/publishers/google/models/{self.model}:generateContent"
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": f"Redesign Intent: {intent}"
                        }
                    ]
                }
            ],
            "systemInstruction": {
                "parts": [
                    {
                        "text": system_instruction
                    }
                ]
            },
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "ux_wireframes": {"type": "STRING"},
                        "css_theme": {
                            "type": "OBJECT",
                            "properties": {
                                "primary": {"type": "STRING"},
                                "secondary": {"type": "STRING"},
                                "fonts": {
                                    "type": "ARRAY",
                                    "items": {"type": "STRING"}
                                }
                            },
                            "required": ["primary", "secondary", "fonts"]
                        },
                        "image_prompts": {
                            "type": "ARRAY",
                            "items": {"type": "STRING"}
                        }
                    },
                    "required": ["ux_wireframes", "css_theme", "image_prompts"]
                }
            }
        }

        with httpx.Client() as client:
            resp = client.post(url, json=payload, headers=headers, timeout=60.0)
            if resp.status_code != 200:
                logger.error(f"Vertex AI API returned error: {resp.status_code} - {resp.text}")
                raise RuntimeError(f"Vertex AI request failed: {resp.text}")
            
            data = resp.json()
            try:
                text_content = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text_content)
            except (KeyError, IndexError, ValueError) as e:
                logger.error(f"Failed to parse model response payload: {e}. Raw response: {data}")
                raise RuntimeError("Invalid model response format")

    def analyze_email_funnel(self, funnel_metrics: str, system_instruction: str) -> dict:
        """Audits email funnel campaigns and spam risk metrics."""
        if os.getenv("AOS_ENV") == "test":
            logger.info("[TEST MODE] Returning mock email funnel analysis")
            if "fail_email" in funnel_metrics:
                return {
                    "spam_risk_score": 75,
                    "passed": False,
                    "ctr_percent": 0.8,
                    "redesign_suggestions": [
                        "CRITICAL: Too many capitalization characters in subject lines",
                        "Spam trigger words detected: 'free', 'buy now'",
                        "Unsub link is missing or hard to read"
                    ]
                }
            return {
                "spam_risk_score": 12,
                "passed": True,
                "ctr_percent": 4.2,
                "redesign_suggestions": [
                    "Excellent CTR. Recommend moving welcome email drip trigger to 5 mins after signup.",
                    "Optimize mobile layout of header logo"
                ]
            }

        import google.auth
        import google.auth.transport.requests

        try:
            credentials, resolved_project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            auth_req = google.auth.transport.requests.Request()
            credentials.refresh(auth_req)
            token = credentials.token
            project = self.project_id or resolved_project
        except Exception as e:
            logger.error(f"Failed to obtain GCP credentials: {e}")
            raise RuntimeError(f"GCP Authentication failed: {e}")

        url = f"https://{self.region}-aiplatform.googleapis.com/v1/projects/{project}/locations/{self.region}/publishers/google/models/{self.model}:generateContent"
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": f"Email Metrics & Funnel Context: {funnel_metrics}"
                        }
                    ]
                }
            ],
            "systemInstruction": {
                "parts": [
                    {
                        "text": system_instruction
                    }
                ]
            },
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "spam_risk_score": {"type": "INTEGER"},
                        "passed": {"type": "BOOLEAN"},
                        "ctr_percent": {"type": "NUMBER"},
                        "redesign_suggestions": {
                            "type": "ARRAY",
                            "items": {"type": "STRING"}
                        }
                    },
                    "required": ["spam_risk_score", "passed", "ctr_percent", "redesign_suggestions"]
                }
            }
        }

        with httpx.Client() as client:
            resp = client.post(url, json=payload, headers=headers, timeout=60.0)
            if resp.status_code != 200:
                logger.error(f"Vertex AI API returned error: {resp.status_code} - {resp.text}")
                raise RuntimeError(f"Vertex AI request failed: {resp.text}")
            
            data = resp.json()
            try:
                text_content = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text_content)
            except (KeyError, IndexError, ValueError) as e:
                logger.error(f"Failed to parse model response payload: {e}. Raw response: {data}")
                raise RuntimeError("Invalid model response format")

    def generate_social_content(self, brand_brief: str, system_instruction: str) -> dict:
        """Drafts social posts and layout specs for social platforms."""
        if os.getenv("AOS_ENV") == "test":
            logger.info("[TEST MODE] Returning mock social content drafts")
            return {
                "instagram_carousel": {
                    "slide_1": "✨ Slide 1: Experience organic hydration like never before.",
                    "slide_2": "💧 Slide 2: Crafted with 100% cold-pressed botanicals.",
                    "visual_layout_spec": "Pastel pink background, gold fonts, soft layout grids."
                },
                "linkedin_post": "🚀 Elevating storefront experiences with optimized conversion funnels. Here is how we scaled our conversion by 15% using clean code principles...",
                "image_prompt": "Stately dropper bottle resting on smooth raw marble block, organic shadow textures, warm studio light, 8k"
            }

        import google.auth
        import google.auth.transport.requests

        try:
            credentials, resolved_project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            auth_req = google.auth.transport.requests.Request()
            credentials.refresh(auth_req)
            token = credentials.token
            project = self.project_id or resolved_project
        except Exception as e:
            logger.error(f"Failed to obtain GCP credentials: {e}")
            raise RuntimeError(f"GCP Authentication failed: {e}")

        url = f"https://{self.region}-aiplatform.googleapis.com/v1/projects/{project}/locations/{self.region}/publishers/google/models/{self.model}:generateContent"
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": f"Brand Brief & Copy Focus: {brand_brief}"
                        }
                    ]
                }
            ],
            "systemInstruction": {
                "parts": [
                    {
                        "text": system_instruction
                    }
                ]
            },
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "instagram_carousel": {
                            "type": "OBJECT",
                            "properties": {
                                "slide_1": {"type": "STRING"},
                                "slide_2": {"type": "STRING"},
                                "visual_layout_spec": {"type": "STRING"}
                            },
                            "required": ["slide_1", "slide_2", "visual_layout_spec"]
                        },
                        "linkedin_post": {"type": "STRING"},
                        "image_prompt": {"type": "STRING"}
                    },
                    "required": ["instagram_carousel", "linkedin_post", "image_prompt"]
                }
            }
        }

        with httpx.Client() as client:
            resp = client.post(url, json=payload, headers=headers, timeout=60.0)
            if resp.status_code != 200:
                logger.error(f"Vertex AI API returned error: {resp.status_code} - {resp.text}")
                raise RuntimeError(f"Vertex AI request failed: {resp.text}")
            
            data = resp.json()
            try:
                text_content = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text_content)
            except (KeyError, IndexError, ValueError) as e:
                logger.error(f"Failed to parse model response payload: {e}. Raw response: {data}")
                raise RuntimeError("Invalid model response format")

    async def generate_personalized_content(
        self,
        tenant_id: str,
        brand_id: str,
        prompt: str,
        session = None,
        system_instruction: Optional[str] = None
    ) -> dict:
        if os.getenv("AOS_ENV") == "test":
            logger.info("[TEST MODE] Returning mock brand identity")
            return {
                "tone_of_voice": "Friendly, modern, and direct",
                "target_persona": "General consumers",
                "past_experience": "No performance logs recorded yet."
            }

        import google.auth
        import google.auth.transport.requests

        try:
            credentials, resolved_project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            auth_req = google.auth.transport.requests.Request()
            credentials.refresh(auth_req)
            token = credentials.token
            project = self.project_id or resolved_project
        except Exception as e:
            logger.error(f"Failed to obtain GCP credentials: {e}")
            raise RuntimeError(f"GCP Authentication failed: {e}")

        url = f"https://{self.region}-aiplatform.googleapis.com/v1/projects/{project}/locations/{self.region}/publishers/google/models/{self.model}:generateContent"
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": prompt
                        }
                    ]
                }
            ],
            "systemInstruction": {
                "parts": [
                    {
                        "text": system_instruction or "You are a senior brand consultant."
                    }
                ]
            },
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "tone_of_voice": {
                            "type": "STRING"
                        },
                        "target_persona": {
                            "type": "STRING"
                        },
                        "past_experience": {
                            "type": "STRING"
                        }
                    },
                    "required": ["tone_of_voice", "target_persona", "past_experience"]
                }
            }
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, headers=headers, timeout=60.0)
            if resp.status_code != 200:
                logger.error(f"Vertex AI API returned error: {resp.status_code} - {resp.text}")
                raise RuntimeError(f"Vertex AI request failed: {resp.text}")
            
            data = resp.json()
            try:
                text_content = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text_content)
            except (KeyError, IndexError, ValueError) as e:
                logger.error(f"Failed to parse model response payload: {e}. Raw response: {data}")
                raise RuntimeError("Invalid model response format")
