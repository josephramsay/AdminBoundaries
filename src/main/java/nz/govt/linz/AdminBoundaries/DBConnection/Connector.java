package nz.govt.linz.AdminBoundaries.DBConnection;

/*
 * This file is part of nz.co.kakariki.NetworkUtilities.
 * 
 * NetworkUtilities is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 3 of the License, or
 * (at your option) any later version.
 * 
 * NetworkUtilities is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 * 
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>.
 */

import java.sql.Connection;
import java.sql.SQLException;
import java.util.Map;


/**
 * Connector interface indicating getConnection method
 * @author jnramsay
 *
 */
public interface Connector {
	//public void closeConnection();
	//public void openConnection();
	//public PreparedStatement prepareStatement(String sql) throws SQLException;
	public Connection getConnection() throws SQLException;
	
	public void init(Map<String,String> params);

}
