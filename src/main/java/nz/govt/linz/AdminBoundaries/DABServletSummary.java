package nz.govt.linz.AdminBoundaries;

/**
 * AdminBoundaries
 *
 * Copyright 2014 Crown copyright (c)
 * Land Information New Zealand and the New Zealand Government.
 * All rights reserved
 *
 * This program is released under the terms of the new BSD license. See the
 * LICENSE file for more information.
 */

import java.io.*;
import java.util.*;

import javax.servlet.*;
import javax.servlet.http.*;

import nz.govt.linz.AdminBoundaries.DABContainerComp.TableInfo;
import nz.govt.linz.AdminBoundaries.DABContainerComp.ImportStatus;

import static nz.govt.linz.AdminBoundaries.DABFormatter.BRED;
import static nz.govt.linz.AdminBoundaries.DABFormatter.BGRN;
//import static nz.govt.linz.AdminBoundaries.DABFormatter.BYLW;

public class DABServletSummary extends DABServlet {

	
	static final long serialVersionUID = 1;
	
	/** Database connector and query wrapper */
	private DABConnector dabc;		
	/** Formatter class for converting data-maps to html strings */
	private DABFormatter dabf;
	/** Class holding info on tables for comparson */
	private DABContainerComp ccomp;
	
	/** Map of the status for each table pair */ 
	private Map<TableInfo,ImportStatus> status = new HashMap<>();
	
	/** Lowest status value across tables for button colouring*/
	private ImportStatus lowstatus = ImportStatus.BLANK;
	
	/**
	 * Initialise servlet class setting status and getting formatter + connector instances
	 */
	public void init() throws ServletException {
		super.init();
		message = "Downloader for Admin Boundarys";
		dabc = new DABConnector();
		dabf = new DABFormatter();
		ccomp = new DABContainerComp();
		//updateStatus();
	}
	
	/**
	 * Initialises the status array, reading table availability
	 */
	private void updateStatus(){
		for (TableInfo ti : ccomp.values()){
			status.put(ti, dabc.getStatus(ti));
		}
		lowstatus = status.values().stream().sorted().findFirst().get();
	}

	
	/**
	 * Starts a new process controller returning the output from the executed script
	 * @param action User provided 
	 * @return
	 */
	public String readProcessOutput(String action){
		//read admin_bdys diffs
		ProcessControl pc = new ProcessControl();
		return pc.startProcessStage(action);
	}
	
	/**
	 * Servlet doGet
	 * @param request
	 * @param response
	 */
	public void doGet(HttpServletRequest request, HttpServletResponse response)
		    throws IOException, ServletException {
		String summarytable = "";
		String accdectable = "";
		String infomessage = "";
		String sp = request.getServletPath();//  getServletContext().getRealPath("/");
		
		response.setContentType("text/html; charset=UTF-8");
        PrintWriter out = response.getWriter();
        
        HttpSession session = request.getSession(true);

        Date created = new Date(session.getCreationTime());
        Date accessed = new Date(session.getLastAccessedTime());
        String user = (String) request.getAttribute("currentSessionUser");

        //String user = request.getParameter("user");
        String compare = request.getParameter("compare");
        String action = request.getParameter("action");
        
        /* If compare action requested for table generate a diff table.
         * If action requested start processcontrol and return result.
         * Otherwise return the standard summary table
         */
        
        Map<String, String> info = new HashMap<>(); 
        
        if (compare != null) {
        	//s = per table compare, a = 1
        	TableInfo ti = ccomp.keyOf(compare.toUpperCase());
        	info.put("COMPARE",compare);
        	info.put("RESULT",ti.dst()+" &larr; "+ti.tmp());
        	summarytable = dabc.compareTableData(ti);
        	accdectable = dabf.getBackNav();
        }
        else if (action != null) {
        	//s = summary, a = 1
            info.put("ACTION",action);
            info.put("RESULT",readProcessOutput(action));
            updateStatus();
            summarytable = getFullSummary();
            accdectable = dabf.getNavigation(lowstatus.ordinal());
        }
        else {
        	updateStatus();
            switch (lowstatus){
            case BLANK: 
            	//show dst table  - <blank>
            	summarytable = ccomp.DEF_TABLE;
            case LOADED:
            case COMPLETE:
            	//show counts match
            	summarytable = getFullSummary();
            default:
            }
            accdectable = dabf.getNavigation(lowstatus.ordinal());
        }
        
        infomessage = dabf.getInfoMessage(info);
        
        //OUTPUT
        out.println(getHTMLWrapper(
                getBodyContent(infomessage,summarytable,accdectable),
                getBodyFooter(created,accessed,user)
                )
        	);
	}
	
	/**
	 * Get all table side-by-side comparison articles
	 * @return
	 */
	private String getFullSummary(){
		String res = "";
		for (String tm_str : ccomp.TABV.keySet()){
			res = res.concat(getSummarySection(ccomp.valueOf(tm_str)));
		}
		return res;
		
	}
	
	/**
	 * Builds table comparison article for a particular tableinfo type
	 * @param ti
	 * @return
	 */
	private String getSummarySection(TableInfo ti){
		ImportStatus is = status.get(ti);
		String b_col,href;
		if (is == ImportStatus.BLANK){
			b_col = BRED;
			href = "/ab";
		}
		else {
			b_col = BGRN;
			href = "sum?compare="+ti.abv();
		}
		String detail = String.join("\n"
				,"<section class=\"detail\">"
				,"<p><a href=\"" + href + "\" class=\""+b_col+"\">Compare "+ti.abv().toUpperCase()+" Tables</a>"
				,ti.ttl(is)+"</p>"
				,"</section>\n");
	    String left = String.join("\n"
	    		,"<section class=\"box\">"
	    		,dabc.compareTableCount(ABs,ti.dst())
	    		,"</section>\n");
	    String right = String.join("\n"
	    	    ,"<section class=\"box\">"
	    	    ,is == ImportStatus.BLANK ? ti.dsp(is) : dabc.compareTableCount(ABIs,ti.dsp(is))
	    	    ,"</section>\n");
	    
	    return "<article>\n" + left + right + detail + "</article>\n";
	    
	}


}
