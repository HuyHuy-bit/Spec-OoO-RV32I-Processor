`default_nettype none
module committed_store (
  input wire clk_i, rst_i, recovery_i,
  input wire desc_valid_i, store_i, uncached_i, fault_i,
  input wire [12:0] id_i,
  input wire [31:0] address_i, write_data_i,
  input wire [3:0] byte_mask_i,
  input wire [1:0] size_i,
  input wire head_valid_i, admit_valid_i,
  input wire [12:0] head_id_i, admit_id_i,
  output wire commit_ready_o,
  input wire commit_i,
  output wire accept_o,
  output wire request_valid_o,
  input wire request_ready_i,
  output memory_protocol_pkg::mem_request_t request_o,
  input wire response_valid_i,
  output wire response_ready_o,
  input memory_protocol_pkg::mem_response_t response_i,
  output wire busy_o,
  output wire [12:0] owner_id_o,
  output wire [31:0] line_address_o, line_mask_o,
  output wire [255:0] line_data_o,
  output logic fatal_o,
  output logic [2:0] fatal_reason_o
);
  import memory_protocol_pkg::*;
  typedef enum logic [1:0] {EMPTY, REQUEST, RESPONSE} state_e;
  state_e state_q;
  logic [12:0] owner_q;
  logic [31:0] address_q, mask_q;
  logic [255:0] data_q;
  logic [3:0] transaction_q;
  logic [2:0] error_reason;
  wire active = !rst_i && !fatal_o;
  wire [3:0] expected_mask = (size_i == 0 ? 4'b0001 : size_i == 1 ? 4'b0011 : 4'b1111)
    << address_i[1:0];
  wire [31:0] data_mask = {{8{byte_mask_i[3]}}, {8{byte_mask_i[2]}},
    {8{byte_mask_i[1]}}, {8{byte_mask_i[0]}}};

  assign commit_ready_o = active && state_q == EMPTY && !recovery_i && !response_valid_i
    && desc_valid_i && store_i && !fault_i && !uncached_i
    && head_valid_i && head_id_i == id_i && admit_valid_i && admit_id_i == id_i;
  assign accept_o = commit_i && commit_ready_o;
  assign request_valid_o = active && state_q == REQUEST;
  assign response_ready_o = active && state_q == RESPONSE;
  assign busy_o = !rst_i && state_q != EMPTY;
  assign owner_id_o = busy_o ? owner_q : 13'd0;
  assign line_address_o = busy_o ? address_q : 32'd0;
  assign line_mask_o = busy_o ? mask_q : 32'd0;
  assign line_data_o = busy_o ? data_q : 256'd0;

  always_comb begin
    request_o = '0;
    if (request_valid_o) begin
      request_o.transaction_id = transaction_q;
      request_o.write = 1;
      request_o.address = address_q;
      request_o.line_write_data = data_q;
      request_o.line_write_mask = mask_q;
    end
    error_reason = 0;
    if (response_valid_i) begin
      if (state_q != RESPONSE) error_reason = 1;
      else if (response_i.transaction_id != transaction_q) error_reason = 2;
      else if (!(response_i.status inside {MEM_STATUS_OK, MEM_STATUS_ACCESS_FAULT})) error_reason = 3;
      else if (response_i.line_read_data != 0 || response_i.uncached_read_data != 0) error_reason = 4;
      else if (response_i.status == MEM_STATUS_ACCESS_FAULT) error_reason = 5;
    end
  end

  // Only retirement acceptance captures bytes; recovery cannot cancel committed ownership.
  always_ff @(posedge clk_i) begin
    if (rst_i) begin
      state_q <= EMPTY;
      transaction_q <= 0;
      fatal_o <= 0;
      fatal_reason_o <= 0;
    end else if (!fatal_o) begin
      if (error_reason != 0) begin
        fatal_o <= 1;
        fatal_reason_o <= error_reason;
      end else case (state_q)
        EMPTY: if (accept_o) begin
          owner_q <= id_i;
          address_q <= {address_i[31:5], 5'd0};
          mask_q <= {28'd0, byte_mask_i} << {address_i[4:2], 2'b00};
          data_q <= {224'd0, write_data_i} << {address_i[4:2], 5'b00000};
          state_q <= REQUEST;
        end
        REQUEST: if (request_ready_i) state_q <= RESPONSE;
        RESPONSE: if (response_valid_i) begin
          transaction_q <= transaction_q + 1'b1;
          state_q <= EMPTY;
        end
        default: ;
      endcase
    end
  end
`ifndef SYNTHESIS
  always_ff @(posedge clk_i) if (!rst_i) begin
    assert (!commit_i || commit_ready_o) else $fatal(1, "STORE_COMMIT_READY");
    if (accept_o) begin
      assert (size_i <= 2) else $fatal(1, "STORE_COMMIT_SIZE");
      assert ((size_i != 1 || !address_i[0]) && (size_i != 2 || address_i[1:0] == 0))
        else $fatal(1, "STORE_COMMIT_ALIGNMENT");
      assert (byte_mask_i == expected_mask) else $fatal(1, "STORE_COMMIT_MASK");
      assert ((write_data_i & ~data_mask) == 0) else $fatal(1, "STORE_COMMIT_DATA");
    end
  end
`endif
endmodule
`default_nettype wire
