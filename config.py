import os

SOLVER_KEY      = os.getenv("SOLVER_KEY", "kontol254535")
MAX_CONCURRENT  = int(os.getenv("MAX_CONCURRENT", "2"))
QUEUE_SIZE      = int(os.getenv("QUEUE_SIZE", "6"))
TASK_TTL_OK     = int(os.getenv("TASK_TTL_OK", "120"))
TASK_TTL_ERR    = int(os.getenv("TASK_TTL_ERR", "90"))
SOLVE_TIMEOUT   = int(os.getenv("SOLVE_TIMEOUT", "90"))
PORT            = int(os.getenv("PORT", "8080"))
