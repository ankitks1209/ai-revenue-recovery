import pandas as pd
from typing import Dict, Any, List
from src.config import load_policy

class PolicyEngine:
    def __init__(self, policy_path: str = None):
        """
        Initialize policy engine by loading intervention policies from config/policy.yaml.
        """
        self.policy_data = load_policy()
        self.policies = self.policy_data.get("policies", {})

    def get_action_for_category(self, category: str) -> Dict[str, str]:
        """
        Retrieve the bounded recovery action and bounds for a given root-cause category.
        Falls back to human escalation for unmapped categories.
        """
        policy = self.policies.get(category, {
            "action": "Escalate to human review",
            "bounds": "Hard stop, zero retries"
        })
        return {
            "chosen_action": policy.get("action", "Escalate to human review"),
            "bounds": policy.get("bounds", "Hard stop, zero retries")
        }

    def apply_policy(self, classified_df: pd.DataFrame) -> pd.DataFrame:
        """
        Join classifier predictions with intervention policies, assigning
        'chosen_action' and 'bounds' to each record.
        """
        df = classified_df.copy()
        if df.empty:
            df["chosen_action"] = []
            df["bounds"] = []
            return df

        actions = []
        bounds = []

        for category in df["predicted_category"]:
            policy_info = self.get_action_for_category(category)
            actions.append(policy_info["chosen_action"])
            bounds.append(policy_info["bounds"])

        df["chosen_action"] = actions
        df["bounds"] = bounds
        return df

    def generate_summary(self, policy_applied_df: pd.DataFrame) -> Dict[str, Any]:
        """
        Generate aggregate summary statistics for the Phase 1 batch:
        - Records per root-cause category (predicted_category)
        - Records per intervention action (chosen_action)
        - Hard stop / escalate to human review count
        """
        if policy_applied_df.empty:
            return {
                "category_counts": {},
                "action_counts": {},
                "escalation_count": 0
            }

        category_counts = policy_applied_df["predicted_category"].value_counts().to_dict()
        action_counts = policy_applied_df["chosen_action"].value_counts().to_dict()

        # Count records requiring escalation / hard stop (non-automated recovery or unknown)
        escalation_keywords = ["escalate", "human review", "Hard stop"]
        escalation_mask = policy_applied_df["chosen_action"].apply(
            lambda act: any(kw.lower() in act.lower() for kw in escalation_keywords)
        )
        escalation_count = int(escalation_mask.sum())

        return {
            "category_counts": category_counts,
            "action_counts": action_counts,
            "escalation_count": escalation_count
        }
