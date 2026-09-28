`default_nettype none
module load_transaction_handshake (
  input wire clk, rst, launch, kill, take,
  input wire [12:0] id, head_id,
  input wire [31:0] address,
  input wire [1:0] size,
  input wire unsigned_load, uncached, head_only, fault, alignment_fault,
  input wire issue_allowed, head_valid, request_ready, send_response, response_fault,
  input wire [255:0] line_data,
  input wire [31:0] uncached_data
);
  import memory_protocol_pkg::*;
  wire ready, request_valid, response_ready, valid, irrevocable, fatal;
  mem_request_t request;
  mem_response_t response;
  logic past_valid = 0;
  logic outstanding, stalled, accepted_last, delayed, canceled, saw_response;
  mem_request_t accepted_request, stalled_request;
  wire request_fire = request_valid && request_ready;
  wire response_valid = !rst && outstanding && send_response;
  wire [31:0] byte_mask = (accepted_request.uncached_size == MEM_SIZE_BYTE ? 32'hff
    : accepted_request.uncached_size == MEM_SIZE_HALFWORD ? 32'hffff : 32'hffffffff)
    << {accepted_request.address[1:0], 3'b000};

  load_transaction dut (
    .clk_i(clk), .rst_i(rst), .kill_i(kill), .launch_i(launch), .ready_o(ready),
    .id_i(id), .address_i(address), .size_i(size), .unsigned_i(unsigned_load),
    .uncached_i(uncached), .head_only_i(head_only), .fault_i(fault),
    .cause_i(alignment_fault ? 32'd4 : 32'd5), .trap_value_i(address),
    .issue_allowed_i(issue_allowed), .head_valid_i(head_valid), .head_id_i(head_id),
    .request_valid_o(request_valid), .request_ready_i(request_ready), .request_o(request),
    .response_valid_i(response_valid), .response_ready_o(response_ready), .response_i(response),
    .valid_o(valid), .take_i(take), .id_o(), .owner_id_o(), .address_o(), .data_o(),
    .cause_o(), .trap_value_o(), .fault_o(), .busy_o(), .irrevocable_o(irrevocable), .fatal_o(fatal)
  );

  always_comb begin
    response = '0;
    response.transaction_id = accepted_request.transaction_id;
    response.status = response_fault ? MEM_STATUS_ACCESS_FAULT : MEM_STATUS_OK;
    if (!response_fault) begin
      if (accepted_request.uncached) response.uncached_read_data = uncached_data & byte_mask;
      else response.line_read_data = line_data;
    end
  end

  // The target records external acceptance, never the DUT's response readiness.
  always @(posedge clk) begin
    past_valid <= 1;
    if (!past_valid) assume (rst);
    if (!rst) begin
      assume (!launch || ready);
      assume (!take || valid);
      assume (!kill || !irrevocable);
      if (launch) begin
        assume (size <= 2 && !(size == 2 && unsigned_load));
        assume (fault || ((size != 1 || !address[0]) && (size != 2 || address[1:0] == 0)));
      end
    end
    if (rst) begin
      outstanding <= 0;
      stalled <= 0;
      accepted_last <= 0;
      delayed <= 0;
      canceled <= 0;
      saw_response <= 0;
    end else begin
      stalled <= request_valid && !request_ready;
      stalled_request <= request;
      accepted_last <= request_fire;
      if (request_valid && kill) canceled <= 1;
      if (outstanding && !response_valid) delayed <= 1;
      if (request_fire) begin
        outstanding <= 1;
        accepted_request <= request;
        delayed <= 0;
      end
      if (response_valid) begin
        outstanding <= 0;
        canceled <= 0;
        saw_response <= 1;
      end
    end

    if (past_valid && !rst) begin
      legal_target_no_fatal: assert (!fatal);
      if (stalled) begin
        held_valid: assert (request_valid);
        held_payload: assert (request == stalled_request);
      end
      response_ownership: assert (response_ready == outstanding);
      no_duplicate_offer: assert (!(outstanding && request_valid));
      cached_accept: cover (request_fire && !request.uncached);
      uncached_accept: cover (request_fire && request.uncached);
      retained_permission: cover (request_fire && issue_allowed && head_valid);
      dropped_permission: cover (request_fire && !issue_allowed && !head_valid);
      uncached_head_dropped: cover (request_fire && request.uncached && !head_valid);
      stalled_request_seen: cover (stalled && request_valid && !request_ready);
      killed_stall: cover (stalled && kill && !request_ready);
      earliest_response: cover (accepted_last && response_valid);
      delayed_response: cover (delayed && response_valid);
      canceled_response: cover (canceled && response_valid);
      kill_response: cover (kill && response_valid);
      response_access_fault: cover (response_valid && response_fault);
      completed_result: cover (valid && take && saw_response);
      next_request: cover (saw_response && request_fire);
    end
    reset_pending: cover (past_valid && rst && outstanding);
  end
endmodule
`default_nettype wire
