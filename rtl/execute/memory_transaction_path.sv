`default_nettype none
module memory_transaction_path (
  input wire clk_i, rst_i, recovery_i, kill_i,
  input wire desc_valid_i,
  output wire desc_take_o,
  input wire [12:0] id_i,
  input wire [31:0] address_i, write_data_i,
  input wire [3:0] byte_mask_i,
  input wire [1:0] size_i,
  input wire store_i, unsigned_i, uncached_i, head_only_i, fault_i,
  input wire [31:0] cause_i, trap_value_i,
  input wire head_valid_i, issue_allowed_i, admit_valid_i,
  input wire [12:0] head_id_i, admit_id_i,
  output wire commit_ready_o,
  input wire commit_i,
  output wire commit_accept_o,
  output wire valid_o,
  input wire take_i,
  output wire [12:0] id_o,
  output wire [31:0] address_o, data_o, write_data_o, cause_o, trap_value_o,
  output wire [3:0] byte_mask_o,
  output wire [1:0] size_o,
  output wire store_o, fault_o,
  output wire busy_o, owner_valid_o, committed_o, irrevocable_o,
  output wire [12:0] owner_id_o,
  output wire fatal_o,
  output wire [3:0] fatal_sources_o,
  output wire [7:0] fatal_reason_o,
  output wire request_valid_o,
  input wire request_ready_i,
  output memory_protocol_pkg::mem_request_t request_o,
  input wire response_valid_i,
  output wire response_ready_o,
  input memory_protocol_pkg::mem_response_t response_i
);
  import memory_protocol_pkg::*;
  wire [2:0] engine_busy, engine_fatal, request_valid, request_ready;
  wire [2:0] response_valid, response_ready, reserve, grant;
  mem_request_t [2:0] requests;
  mem_response_t response;
  wire port_busy, port_fatal;
  wire [327:0] unused;
  assign unused[327] = grant[1];
  wire [1:0] port_reason;
  wire [2:0] cs_reason, mmio_reason;
  wire load_ready, mmio_ready, cs_ready, load_valid, mmio_valid, mmio_authorize;
  wire [12:0] load_id, load_owner, cs_owner, mmio_id, mmio_owner;
  wire [31:0] load_address, load_data, load_cause, load_trap;
  wire [31:0] mmio_address, mmio_data, mmio_cause, mmio_trap;
  wire [3:0] mmio_mask;
  wire [1:0] mmio_size;
  wire load_fault, mmio_fault, mmio_irrevocable;
  logic pending_q, load_uncached_q, load_offered_q;
  logic [1:0] load_size_q;
  logic [3:0] load_mask_q;
  wire active = !rst_i && !fatal_o;
  wire occupied = (|engine_busy) || port_busy || fatal_o;
  wire room = active && !occupied && !recovery_i && !kill_i;
  wire cancel = active && kill_i && !irrevocable_o;
  wire launch_load = room && desc_valid_i && !store_i && load_ready;
  wire launch_mmio = room && desc_valid_i && store_i && (uncached_i || fault_i) && mmio_ready;
  wire cached_desc = room && desc_valid_i && store_i && !uncached_i && !fault_i;
  wire can_reserve = active && pending_q && head_valid_i && issue_allowed_i && !kill_i && !recovery_i;
  wire result_head = head_valid_i && head_id_i == (load_valid ? load_id : mmio_id);

  assign fatal_sources_o = rst_i ? 4'd0 : {port_fatal, engine_fatal};
  assign fatal_o = |fatal_sources_o;
  assign fatal_reason_o = rst_i ? 8'd0 : {port_reason, mmio_reason, cs_reason};
  assign busy_o = !rst_i && occupied;
  assign owner_valid_o = !rst_i && |engine_busy;
  assign owner_id_o = engine_busy[0] ? load_owner : engine_busy[1] ? cs_owner : mmio_owner;
  assign committed_o = engine_busy[1];
  assign irrevocable_o = !rst_i && (load_offered_q || mmio_irrevocable);
  assign reserve[0] = can_reserve && engine_busy[0] && head_id_i == load_owner;
  assign reserve[1] = request_valid[1];
  assign reserve[2] = can_reserve && engine_busy[2] && head_id_i == mmio_owner;
  assign desc_take_o = launch_load || launch_mmio || commit_accept_o;
  assign commit_ready_o = active && !response_valid_i && cs_ready;
  assign valid_o = active && !response_valid_i && !cancel && result_head && (load_valid || mmio_valid);
  assign id_o = valid_o ? (load_valid ? load_id : mmio_id) : 13'd0;
  assign address_o = valid_o ? (load_valid ? load_address : mmio_address) : 32'd0;
  assign data_o = valid_o && load_valid ? load_data : 32'd0;
  assign write_data_o = valid_o && mmio_valid ? mmio_data : 32'd0;
  assign size_o = valid_o ? (load_valid ? load_size_q : mmio_size) : 2'd0;
  assign store_o = valid_o && mmio_valid;
  assign fault_o = valid_o && (load_valid ? load_fault : mmio_fault);
  assign byte_mask_o = valid_o && !fault_o ? (load_valid ? load_mask_q : mmio_mask) : 4'd0;
  assign cause_o = valid_o ? (load_valid ? load_cause : mmio_cause) : 32'd0;
  assign trap_value_o = valid_o ? (load_valid ? load_trap : mmio_trap) : 32'd0;

  // The engines own occupancy; pending tracks only an ungranted bus reservation.
  always_ff @(posedge clk_i) begin
    if (rst_i) begin
      pending_q <= 0;
      load_offered_q <= 0;
    end else if (active) begin
      if (cancel || grant[0] || grant[2]) pending_q <= 0;
      if (launch_load || launch_mmio) pending_q <= !fault_i;
      if (launch_load) begin
        load_uncached_q <= uncached_i;
        load_size_q <= size_i;
        load_mask_q <= byte_mask_i;
      end
      if (grant[0] && load_uncached_q) load_offered_q <= 1;
      if (take_i && valid_o && load_valid) load_offered_q <= 0;
    end
  end

  load_transaction load_engine (
    .clk_i, .rst_i, .kill_i(cancel), .launch_i(launch_load), .ready_o(load_ready),
    .id_i, .address_i, .size_i, .unsigned_i, .uncached_i, .head_only_i, .fault_i, .cause_i, .trap_value_i,
    .issue_allowed_i(grant[0]), .head_valid_i, .head_id_i,
    .request_valid_o(request_valid[0]), .request_ready_i(request_ready[0]), .request_o(requests[0]),
    .response_valid_i(response_valid[0]), .response_ready_o(response_ready[0]), .response_i(response),
    .valid_o(load_valid), .take_i(take_i && valid_o && load_valid),
    .id_o(load_id), .owner_id_o(load_owner), .address_o(load_address), .data_o(load_data),
    .cause_o(load_cause), .trap_value_o(load_trap), .fault_o(load_fault),
    .busy_o(engine_busy[0]), .irrevocable_o(unused[0]), .fatal_o(engine_fatal[0])
  );
  committed_store committed_engine (
    .clk_i, .rst_i, .recovery_i(recovery_i || kill_i), .desc_valid_i(cached_desc),
    .store_i, .uncached_i, .fault_i, .id_i, .address_i, .write_data_i, .byte_mask_i, .size_i,
    .head_valid_i, .admit_valid_i, .head_id_i, .admit_id_i,
    .commit_ready_o(cs_ready), .commit_i(commit_i && commit_ready_o), .accept_o(commit_accept_o),
    .request_valid_o(request_valid[1]), .request_ready_i(request_ready[1]), .request_o(requests[1]),
    .response_valid_i(response_valid[1]), .response_ready_o(response_ready[1]), .response_i(response),
    .busy_o(engine_busy[1]), .owner_id_o(cs_owner), .line_address_o(unused[32:1]), .line_mask_o(unused[64:33]), .line_data_o(unused[320:65]),
    .fatal_o(engine_fatal[1]), .fatal_reason_o(cs_reason)
  );
  mmio_store_transaction mmio_engine (
    .clk_i, .rst_i, .kill_i(cancel), .launch_i(launch_mmio), .ready_o(mmio_ready),
    .id_i, .address_i, .write_data_i, .byte_mask_i, .size_i, .store_i, .uncached_i, .fault_i, .cause_i, .trap_value_i,
    .issue_allowed_i(grant[2]), .head_valid_i, .head_id_i, .authorize_o(mmio_authorize),
    .request_valid_o(request_valid[2]), .request_ready_i(request_ready[2]), .request_o(requests[2]),
    .response_valid_i(response_valid[2]), .response_ready_o(response_ready[2]), .response_i(response),
    .valid_o(mmio_valid), .take_i(take_i && valid_o && mmio_valid),
    .id_o(mmio_id), .owner_id_o(mmio_owner), .address_o(mmio_address), .write_data_o(mmio_data),
    .cause_o(mmio_cause), .trap_value_o(mmio_trap), .byte_mask_o(mmio_mask), .size_o(mmio_size),
    .fault_o(mmio_fault), .busy_o(engine_busy[2]), .irrevocable_o(mmio_irrevocable),
    .fatal_o(engine_fatal[2]), .fatal_reason_o(mmio_reason)
  );
  data_port_arbiter port (
    .clk_i, .rst_i, .reserve_i(reserve), .client_fatal_i(engine_fatal), .grant_o(grant), .owner_o(unused[323:321]),
    .client_request_valid_i(request_valid), .client_request_ready_o(request_ready), .client_request_i(requests),
    .client_response_valid_o(response_valid), .client_response_ready_i(response_ready), .client_response_o(response),
    .request_valid_o, .request_ready_i, .request_o, .response_valid_i, .response_ready_o, .response_i,
    .busy_o(port_busy), .fatal_o(port_fatal), .fatal_reason_o(port_reason), .fatal_clients_o(unused[326:324])
  );
`ifndef SYNTHESIS
  wire [3:0] expected_load_mask = fault_i ? 4'd0 :
    ((size_i == 0 ? 4'b0001 : size_i == 1 ? 4'b0011 : 4'b1111) << address_i[1:0]);
  always_ff @(posedge clk_i) if (!rst_i) begin
    assert (!(kill_i && take_i)) else $fatal(1, "MEM_PATH_KILL_TAKE");
    assert (!launch_load || byte_mask_i == expected_load_mask) else $fatal(1, "MEM_PATH_LOAD_MASK");
    assert (!take_i || valid_o) else $fatal(1, "MEM_PATH_TAKE");
    assert (!commit_i || commit_ready_o) else $fatal(1, "MEM_PATH_COMMIT");
    assert (!kill_i || !irrevocable_o) else $fatal(1, "MEM_PATH_MMIO_KILL");
    assert ($onehot0(engine_busy)) else $fatal(1, "MEM_PATH_SINGLE_OWNER");
    assert (grant[2] == mmio_authorize) else $fatal(1, "MEM_PATH_MMIO_GRANT");
    if (active && pending_q) begin
      assert ((engine_busy[0] || engine_busy[2]) && !load_valid && !mmio_valid
        && !request_valid[0] && !request_valid[2] && !response_ready[0] && !response_ready[2])
        else $fatal(1, "MEM_PATH_PENDING");
    end
  end
`endif
endmodule
`default_nettype wire
