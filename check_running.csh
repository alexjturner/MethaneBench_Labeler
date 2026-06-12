#!/bin/tcsh
set check=`ps uxwww | grep uvicorn|grep -v grep`
if ("$check" == "") then
/home/funnel/bench4/start_app.csh
endif
