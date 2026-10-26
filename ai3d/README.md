# AI3D GPU pool companion

This directory versions the GPU queue scheduler used by the separate AI3D service on the Max AI server.

- Production source: `/home/dashboardadmin/ai3d/studio/services/gpu_pool.py`
- Repository copy: `ai3d/studio/services/gpu_pool.py`
- The AI3D service is deployed and managed separately from the Strata application in this repository. This snapshot records the live scheduler source; it does not by itself deploy or restart the AI3D service.

The scheduler preserves home GPU assignments while allowing GPUs to help other model queues. TRELLIS can use GPUs 1, 2, 3, then 0; GPU 3 has a memory threshold because OpenJEV remains resident there, and large PBR jobs are excluded from GPU 3 after an observed out-of-memory failure.
