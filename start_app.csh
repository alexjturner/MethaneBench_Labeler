#!/bin/tcsh
cd /home/funnel/bench4
/bin/rm -f app.log
setenv LD_LIBRARY_PATH
source /opt/intel/oneapi/setup.csh
setenv MPLCONFIGDIR /tmp
uvicorn app:app --host=localhost --port=63951 >&app.log&
