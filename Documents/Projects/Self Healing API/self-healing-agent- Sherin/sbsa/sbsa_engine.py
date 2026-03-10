import numpy as np
from sentence_transformers import SentenceTransformer
from scipy.optimize import linear_sum_assignment

class SBSAEngine:
    def __init__(self, model_name='all-MiniLM-L6-v2', threshold=0.2):
        """
        SBSA (Semantic Bridge for Structured Alignment) Engine
        
        Args:
            model_name: The SentenceTransformer model to use.
            threshold: Minimum similarity score (0-1) to accept a mapping.
                      Default 0.2 allows flexible matching for schema drift scenarios.
                      Empirically, valid mappings like 'location'→'geo_identifier' score ~0.27.
        """
        # Using MiniLM-L6: High performance, low latency (~30-50ms)
        self.model = SentenceTransformer(model_name)
        self.threshold = threshold

    def find_mapping(self, agent_keys, api_keys):
        """
        Finds the optimal semantic mapping between agent intent and API schema.
        
        Args:
            agent_keys (list): Keys produced by the LLM (e.g., ['location', 'temp_unit'])
            api_keys (list): Keys required by the ToolBench JSON (e.g., ['city', 'units'])
            
        Returns:
            dict: A mapping of {agent_key: api_key}
        """
        if not agent_keys or not api_keys:
            return {}

        # 1. Vectorize keys with normalization for direct Cosine Similarity calculation
        agent_vecs = self.model.encode(agent_keys, normalize_embeddings=True)
        api_vecs = self.model.encode(api_keys, normalize_embeddings=True)

        # 2. Compute Similarity Matrix (Dot Product)
        # Result matrix [i][j] is the similarity between agent_key[i] and api_key[j]
        sim_matrix = np.dot(agent_vecs, api_vecs.T)
        
        # 3. Compute Cost Matrix for Hungarian Algorithm (1 - Similarity)
        # We minimize cost to maximize semantic alignment
        cost_matrix = 1 - sim_matrix

        # 4. Solve the Linear Sum Assignment (Hungarian Algorithm)
        # This finds the best global alignment for the entire set of keys
        row_ind, col_ind = linear_sum_assignment(cost_matrix)

        # 5. Filter matches by confidence threshold
        translation_map = {}
        for r, c in zip(row_ind, col_ind):
            similarity_score = sim_matrix[r][c]
            
            if similarity_score >= self.threshold:
                translation_map[agent_keys[r]] = api_keys[c]
                # Optional: print(f"Aligned {agent_keys[r]} -> {api_keys[c]} ({similarity_score:.2f})")
            else:
                # Log non-matches to avoid silent "forced" errors
                print(f"[SBSA] Warning: Weak match ignored for '{agent_keys[r]}'")

        return translation_map